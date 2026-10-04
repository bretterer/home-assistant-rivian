"""Automated Turnkey Dashboard Generator for Rivian Trip Efficiency & Analytics."""

from __future__ import annotations

from collections.abc import Callable
import copy
import functools
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .charge_curves import DCFC_REFERENCE_CURVES  # noqa: F401  (re-export)
from .config_flow import CONF_CHART_WINDOW_DAYS, DEFAULT_CHART_WINDOW_DAYS
from .const import ATTR_VEHICLE, DASHBOARD_SCHEMA_VERSION, DOMAIN
from .demo import get_demo_vehicles

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DEFAULT_DASHBOARD_ID = "rivian_dashboard"
DEFAULT_URL_PATH = "rivian-dashboard"
DEFAULT_TITLE = "Rivian"
DEFAULT_ICON = "mdi:car-electric"
AUTOMOBILES_URL_PATH = "dashboard-automobiles"
AUTOMOBILES_STORAGE_KEY = "lovelace.dashboard_automobiles"

# Card types that fetch bulk analytics series over WebSocket (see
# _wrap_series_card) or embed the drive-explorer element; used both to find
# "our" charts for the dashboard-automobiles injection and to keep the
# in-place chart swap working across regenerations.
CHART_CARD_TYPES: Final = ("custom:plotly-graph", "custom:rivian-series-card")


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


async def _async_load_dashboard_config(
    hass: HomeAssistant, url_path: str, storage_key: str
) -> dict[str, Any] | None:
    """Load a dashboard's current config, preferring Lovelace's live copy."""
    if (dashboard := _live_storage_dashboard(hass, url_path)) is not None:
        try:
            # Copy so edits never touch Lovelace's cached config before saving.
            return copy.deepcopy(await dashboard.async_load(False))
        except HomeAssistantError:  # no config saved for this dashboard yet
            return None
    data = await Store(hass, 1, storage_key).async_load()
    return data.get("config") if data else None


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


DCFC_SESSION_COLORS: list[str] = [
    "#00E5FF",  # 1. Electric Cyan
    "#E040FB",  # 2. Neon Magenta
    "#FF9100",  # 3. Vivid Orange
    "#FFD600",  # 4. Bright Yellow
    "#FF5252",  # 5. Coral Red
    "#7C4DFF",  # 6. Deep Violet
    "#00E676",  # 7. Spring Green
    "#FF4081",  # 8. Hot Pink
    "#40C4FF",  # 9. Sky Blue
    "#AEEA00",  # 10. Neon Lime
]


def _wrap_series_card(
    vin: str,
    series: list[str],
    card: dict[str, Any],
    days: int = DEFAULT_CHART_WINDOW_DAYS,
) -> dict[str, Any]:
    """Wrap a Plotly card so it pulls bulk series data via WebSocket instead of attributes.

    The wrapper card fetches window.__rivianAnalytics[vin] over WebSocket before
    handing `card` off to the unmodified vendored plotly-graph element. `days`
    is the analytics history window (`CONF_CHART_WINDOW_DAYS`) the card asks
    the `rivian/analytics/series` command for.
    """
    return {
        "type": "custom:rivian-series-card",
        "vin": vin,
        "series": series,
        "days": days,
        "card": card,
    }


def _build_vehicle_analytics_view(
    vehicle_name: str,
    entity_prefix: str,
    vin: str,
    days: int = DEFAULT_CHART_WINDOW_DAYS,
) -> dict[str, Any]:
    """Build every analytics chart/hero card (Plotly & Mushroom) for one vehicle.

    This aggregates ALL of the bulk-series charts (temperature/distance/speed
    scatterplots, box plots, vampire-drain charts, DCFC curves) plus the hero
    summary and stats grid into one flat "cards" list, in the order they were
    historically laid out on a single "<Vehicle> Efficiency" view. The actual
    5-tab dashboard (`async_create_efficiency_dashboard`) slices this list
    across the Efficiency tab instead of using it as a view
    directly (see `_build_efficiency_cards`; the
    Overview tab no longer uses this at all -- see `_build_overview_view`);
    this function is kept as a single source of truth for that content and
    for tests that need "every chart for a vehicle" in one place (e.g. the
    WebSocket field-coverage tests).
    """
    wrap_series_card = functools.partial(_wrap_series_card, days=days)
    eff_30d_entity = f"{entity_prefix}efficiency_30_days"
    last_eff_entity = f"{entity_prefix}last_drive_efficiency"
    last_dist_entity = f"{entity_prefix}last_drive_distance"
    last_mpge_entity = f"{entity_prefix}last_drive_mpge"
    mpge_30d_entity = f"{entity_prefix}mpge_30_days"
    mpge_all_entity = f"{entity_prefix}mpge_all_time"
    eff_all_entity = f"{entity_prefix}efficiency_all_time"
    status_entity = f"{entity_prefix}drive_status"
    battery_cap_entity = f"{entity_prefix}battery_capacity"
    battery_cap_fallback = (
        f"sensor.{vehicle_name.lower().replace(' ', '_')}_battery_capacity"
    )

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
                            "{% if eff >= 2.8 %}green{% elif eff >= 2.2 %}amber{% else %}red{% endif %}"
                        ),
                        "badge_icon": (
                            f"{{% if is_state('{status_entity}', 'Driving') %}}mdi:car-electric"
                            "{% else %}mdi:car-parking-lights{% endif %}"
                        ),
                        "badge_color": (
                            f"{{% if is_state('{status_entity}', 'Driving') %}}green{{% else %}}blue{{% endif %}}"
                        ),
                        "tap_action": {
                            "action": "more-info",
                            "entity": last_eff_entity,
                        },
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
                                "tap_action": {
                                    "action": "more-info",
                                    "entity": eff_30d_entity,
                                },
                            },
                            {
                                "type": "template",
                                "icon": "mdi:calendar-range",
                                "icon_color": "cyan",
                                "content": (
                                    f"90-Day: {{{{ (state_attr('{eff_30d_entity}', 'stats_90d') or {{}}).efficiency_mi_kwh | default(states('{eff_30d_entity}'), true) }}}} mi/kWh "
                                    f"({{{{ (state_attr('{eff_30d_entity}', 'stats_90d') or {{}}).mpge | default(states('{mpge_30d_entity}'), true) }}}} MPGe)"
                                ),
                                "entity": eff_30d_entity,
                                "tap_action": {
                                    "action": "more-info",
                                    "entity": eff_30d_entity,
                                },
                            },
                            {
                                "type": "template",
                                "icon": "mdi:calendar-star",
                                "icon_color": "amber",
                                "content": (
                                    f"365-Day: {{{{ (state_attr('{eff_30d_entity}', 'stats_365d') or {{}}).efficiency_mi_kwh | default(states('{eff_all_entity}'), true) }}}} mi/kWh "
                                    f"({{{{ (state_attr('{eff_30d_entity}', 'stats_365d') or {{}}).mpge | default(states('{mpge_all_entity}'), true) }}}} MPGe)"
                                ),
                                "entity": eff_30d_entity,
                                "tap_action": {
                                    "action": "more-info",
                                    "entity": eff_30d_entity,
                                },
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
                                "tap_action": {
                                    "action": "more-info",
                                    "entity": eff_all_entity,
                                },
                            },
                            {
                                "type": "template",
                                "icon": "mdi:gas-station-off",
                                "icon_color": "teal",
                                "content": f"Last MPGe: {{{{ states('{last_mpge_entity}') }}}}",
                                "entity": last_mpge_entity,
                                "tap_action": {
                                    "action": "more-info",
                                    "entity": last_mpge_entity,
                                },
                            },
                            {
                                "type": "entity",
                                "entity": status_entity,
                                "icon": "mdi:car",
                                "icon_color": "blue",
                                "tap_action": {
                                    "action": "more-info",
                                    "entity": status_entity,
                                },
                            },
                        ],
                    },
                ],
            },
            # Section 2: Temperature vs. Efficiency Scatterplot (Elevation Color-Coded)
            wrap_series_card(
                vin,
                ["drives"],
                {
                    "type": "custom:plotly-graph",
                    "raw_plotly_config": True,
                    "title": "Temperature vs. Efficiency (Elevation Color-Coded)",
                    "layout": {
                        "xaxis": {
                            "title": "Ambient Route Temperature (°F)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "yaxis": {
                            "title": "Efficiency (mi/kWh)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "legend": {"orientation": "h", "y": -0.25, "x": 0.05},
                        "margin": {"l": 50, "r": 20, "t": 40, "b": 60},
                    },
                    "config": {"displayModeBar": False},
                    "entities": [
                        {
                            "name": "Downhill (Δh < -100 ft)",
                            "type": "scatter",
                            "mode": "markers",
                            "marker": {
                                "color": "#1E88E5",
                                "symbol": "circle",
                                "opacity": 0.85,
                                "line": {"width": 1, "color": "#ffffff"},
                                "size": (
                                    f"$ex (function() {{ "
                                    f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                    "return drives.filter(d => d.elevation_change_ft < -100).map(d => Math.max(7, Math.min(32, Math.round(7 + (d.distance || 0) * 1.8)))); "
                                    "})()"
                                ),
                            },
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft < -100).map(d => [d.distance, d.elevation_change_ft]); "
                                "})()"
                            ),
                            "hovertemplate": "<b>Downhill Drive</b><br>Temperature: %{x}°F<br>Efficiency: %{y:.2f} mi/kWh<br>Trip Distance: %{customdata[0]:.1f} mi<br>Elevation Δh: %{customdata[1]:+.0f} ft<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft < -100).map(d => d.temp_f ?? 70); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft < -100).map(d => d.efficiency); "
                                "})()"
                            ),
                        },
                        {
                            "name": "Flat (-100 to +100 ft)",
                            "type": "scatter",
                            "mode": "markers",
                            "marker": {
                                "color": "#43A047",
                                "symbol": "circle",
                                "opacity": 0.85,
                                "line": {"width": 1, "color": "#ffffff"},
                                "size": (
                                    f"$ex (function() {{ "
                                    f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                    "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => Math.max(7, Math.min(32, Math.round(7 + (d.distance || 0) * 1.8)))); "
                                    "})()"
                                ),
                            },
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => [d.distance, d.elevation_change_ft]); "
                                "})()"
                            ),
                            "hovertemplate": "<b>Flat Drive</b><br>Temperature: %{x}°F<br>Efficiency: %{y:.2f} mi/kWh<br>Trip Distance: %{customdata[0]:.1f} mi<br>Elevation Δh: %{customdata[1]:+.0f} ft<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => d.temp_f ?? 70); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => d.efficiency); "
                                "})()"
                            ),
                        },
                        {
                            "name": "Uphill (Δh > +100 ft)",
                            "type": "scatter",
                            "mode": "markers",
                            "marker": {
                                "color": "#FB8C00",
                                "symbol": "circle",
                                "opacity": 0.85,
                                "line": {"width": 1, "color": "#ffffff"},
                                "size": (
                                    f"$ex (function() {{ "
                                    f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                    "return drives.filter(d => d.elevation_change_ft > 100).map(d => Math.max(7, Math.min(32, Math.round(7 + (d.distance || 0) * 1.8)))); "
                                    "})()"
                                ),
                            },
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft > 100).map(d => [d.distance, d.elevation_change_ft]); "
                                "})()"
                            ),
                            "hovertemplate": "<b>Uphill Drive</b><br>Temperature: %{x}°F<br>Efficiency: %{y:.2f} mi/kWh<br>Trip Distance: %{customdata[0]:.1f} mi<br>Elevation Δh: %{customdata[1]:+.0f} ft<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft > 100).map(d => d.temp_f ?? 70); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft > 100).map(d => d.efficiency); "
                                "})()"
                            ),
                        },
                    ],
                },
            ),
            # Section 3: Drive Distance vs. Efficiency Scatterplot (Full Drives)
            wrap_series_card(
                vin,
                ["drives"],
                {
                    "type": "custom:plotly-graph",
                    "raw_plotly_config": True,
                    "title": "Drive Distance vs. Efficiency",
                    "layout": {
                        "xaxis": {
                            "title": "Drive Distance (miles)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "yaxis": {
                            "title": "Efficiency (mi/kWh)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "legend": {"orientation": "h", "y": -0.25, "x": 0.05},
                        "margin": {"l": 50, "r": 20, "t": 40, "b": 60},
                    },
                    "config": {"displayModeBar": False},
                    "entities": [
                        {
                            "name": "Downhill (Δh < -100 ft)",
                            "type": "scatter",
                            "mode": "markers",
                            "marker": {
                                "color": "#1E88E5",
                                "symbol": "circle",
                                "size": 8,
                                "opacity": 0.85,
                                "line": {"width": 1, "color": "#ffffff"},
                            },
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft < -100).map(d => [d.temp_f ?? 70, d.elevation_change_ft]); "
                                "})()"
                            ),
                            "hovertemplate": "<b>Downhill Drive (o)</b><br>Distance: %{x:.2f} mi<br>Efficiency: %{y:.2f} mi/kWh<br>Temp: %{customdata[0]:.1f}°F<br>Elevation Δh: %{customdata[1]:+.0f} ft<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft < -100).map(d => d.distance); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft < -100).map(d => d.efficiency); "
                                "})()"
                            ),
                        },
                        {
                            "name": "Flat (-100 to +100 ft)",
                            "type": "scatter",
                            "mode": "markers",
                            "marker": {
                                "color": "#43A047",
                                "symbol": "circle",
                                "size": 8,
                                "opacity": 0.85,
                                "line": {"width": 1, "color": "#ffffff"},
                            },
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => [d.temp_f ?? 70, d.elevation_change_ft]); "
                                "})()"
                            ),
                            "hovertemplate": "<b>Flat Drive</b><br>Distance: %{x:.2f} mi<br>Efficiency: %{y:.2f} mi/kWh<br>Temp: %{customdata[0]:.1f}°F<br>Elevation Δh: %{customdata[1]:+.0f} ft<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => d.distance); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft >= -100 && d.elevation_change_ft <= 100).map(d => d.efficiency); "
                                "})()"
                            ),
                        },
                        {
                            "name": "Uphill (Δh > +100 ft)",
                            "type": "scatter",
                            "mode": "markers",
                            "marker": {
                                "color": "#FB8C00",
                                "symbol": "cross",
                                "size": 8,
                                "opacity": 0.85,
                                "line": {"width": 1, "color": "#ffffff"},
                            },
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft > 100).map(d => [d.temp_f ?? 70, d.elevation_change_ft]); "
                                "})()"
                            ),
                            "hovertemplate": "<b>Uphill Drive (+)</b><br>Distance: %{x:.2f} mi<br>Efficiency: %{y:.2f} mi/kWh<br>Temp: %{customdata[0]:.1f}°F<br>Elevation Δh: %{customdata[1]:+.0f} ft<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft > 100).map(d => d.distance); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const drives = (window.__rivianAnalytics?.['{vin}']?.drives || []); "
                                "return drives.filter(d => d.elevation_change_ft > 100).map(d => d.efficiency); "
                                "})()"
                            ),
                        },
                    ],
                },
            ),
            # Section 4: Speed Bin Distribution Bar Chart (Total Miles per 10 mph Bin)
            wrap_series_card(
                vin,
                ["speed_bins"],
                {
                    "type": "custom:plotly-graph",
                    "raw_plotly_config": True,
                    "title": "Speed Bin Distribution (Total Miles, All Stored Drives)",
                    "layout": {
                        "xaxis": {
                            "title": "Speed Range (mph)",
                            "type": "category",
                            "tickmode": "array",
                            "tickvals": [
                                "0-9",
                                "10-19",
                                "20-29",
                                "30-39",
                                "40-49",
                                "50-59",
                                "60-69",
                                "70-79",
                                "80+",
                            ],
                        },
                        "yaxis": {
                            "title": "Total Miles",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "margin": {"l": 50, "r": 20, "t": 40, "b": 50},
                    },
                    "config": {"displayModeBar": False},
                    "entities": [
                        {
                            "name": "Miles in Speed Bin",
                            "type": "bar",
                            "marker": {
                                "color": "#26A69A",
                                "line": {"width": 1, "color": "#ffffff"},
                            },
                            "hovertemplate": "Speed Bin: %{x} mph<br>Total Distance: %{y:.1f} miles<extra></extra>",
                            "x": [
                                "0-9",
                                "10-19",
                                "20-29",
                                "30-39",
                                "40-49",
                                "50-59",
                                "60-69",
                                "70-79",
                                "80+",
                            ],
                            # Totals are summed server-side over every retained
                            # drive. No fallback to the last drive's attribute:
                            # that masked a broken feed as plausible-looking data.
                            "y": (
                                f"$ex (function() {{ "
                                f"const totals = (window.__rivianAnalytics?.['{vin}']?.speed_bins || {{}}); "
                                "const keys = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                "return keys.map(k => Math.round(((totals[k] || {}).miles || 0) * 10) / 10); "
                                "})()"
                            ),
                        }
                    ],
                },
            ),
            # Section 5: Efficiency Distribution by Speed Range (Box Plot)
            wrap_series_card(
                vin,
                ["chunks"],
                {
                    "type": "custom:plotly-graph",
                    "raw_plotly_config": True,
                    "title": "Efficiency Distribution by Speed Range (Box Plot)",
                    "layout": {
                        "xaxis": {
                            "title": "Speed Range (mph)",
                            "type": "category",
                            "categoryorder": "array",
                            "categoryarray": [
                                "0-9",
                                "10-19",
                                "20-29",
                                "30-39",
                                "40-49",
                                "50-59",
                                "60-69",
                                "70-79",
                                "80+",
                            ],
                            "tickmode": "array",
                            "tickvals": [
                                "0-9",
                                "10-19",
                                "20-29",
                                "30-39",
                                "40-49",
                                "50-59",
                                "60-69",
                                "70-79",
                                "80+",
                            ],
                        },
                        "yaxis": {
                            "title": "Efficiency per 3-min chunk (mi/kWh)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "boxmode": "group",
                        "boxgroupgap": 0.1,
                        "boxgap": 0.15,
                        "legend": {"orientation": "h", "y": -0.25, "x": 0.05},
                        "margin": {"l": 50, "r": 20, "t": 40, "b": 60},
                    },
                    "config": {"displayModeBar": False},
                    "entities": [
                        {
                            "name": "Uphill (+)",
                            "type": "box",
                            "boxpoints": "all",
                            "jitter": 0.35,
                            "pointpos": 0,
                            "boxmean": True,
                            "marker": {
                                "symbol": "cross",
                                "size": 6,
                                "color": "#FF9800",
                                "opacity": 0.8,
                            },
                            "line": {"color": "#FF9800", "width": 1.5},
                            "fillcolor": "rgba(255, 152, 0, 0.25)",
                            "hovertemplate": "<b>Uphill chunk (+)</b><br>Speed Range: %{x} mph<br>Efficiency: %{y:.2f} mi/kWh<br>Avg Speed: %{customdata[0]:.1f} mph<br>Distance: %{customdata[1]:.2f} mi (%{customdata[2]:.0f}s)<br>Elevation Δh: %{customdata[3]:+.0f} ft<br>Temp: %{customdata[4]:.1f}°F<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft >= 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => s.speed_bin); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft >= 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => s.efficiency_mi_kwh); "
                                "})()"
                            ),
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft >= 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => [s.avg_speed_mph, s.distance_miles, s.duration_seconds, s.elevation_change_ft, s.temp_f || 70]); "
                                "})()"
                            ),
                        },
                        {
                            "name": "Downhill (o)",
                            "type": "box",
                            "boxpoints": "all",
                            "jitter": 0.35,
                            "pointpos": 0,
                            "boxmean": True,
                            "marker": {
                                "symbol": "circle",
                                "size": 6,
                                "color": "#2196F3",
                                "opacity": 0.8,
                            },
                            "line": {"color": "#2196F3", "width": 1.5},
                            "fillcolor": "rgba(33, 150, 243, 0.25)",
                            "hovertemplate": "<b>Downhill chunk (o)</b><br>Speed Range: %{x} mph<br>Efficiency: %{y:.2f} mi/kWh<br>Avg Speed: %{customdata[0]:.1f} mph<br>Distance: %{customdata[1]:.2f} mi (%{customdata[2]:.0f}s)<br>Elevation Δh: %{customdata[3]:+.0f} ft<br>Temp: %{customdata[4]:.1f}°F<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft < 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => s.speed_bin); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft < 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => s.efficiency_mi_kwh); "
                                "})()"
                            ),
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft < 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => [s.avg_speed_mph, s.distance_miles, s.duration_seconds, s.elevation_change_ft, s.temp_f || 70]); "
                                "})()"
                            ),
                        },
                    ],
                },
            ),
            # Section 6: MPGe Distribution by Speed Range (Box Plot)
            wrap_series_card(
                vin,
                ["chunks"],
                {
                    "type": "custom:plotly-graph",
                    "raw_plotly_config": True,
                    "title": "MPGe Distribution by Speed Range (Box Plot)",
                    "layout": {
                        "xaxis": {
                            "title": "Speed Range (mph)",
                            "type": "category",
                            "categoryorder": "array",
                            "categoryarray": [
                                "0-9",
                                "10-19",
                                "20-29",
                                "30-39",
                                "40-49",
                                "50-59",
                                "60-69",
                                "70-79",
                                "80+",
                            ],
                            "tickmode": "array",
                            "tickvals": [
                                "0-9",
                                "10-19",
                                "20-29",
                                "30-39",
                                "40-49",
                                "50-59",
                                "60-69",
                                "70-79",
                                "80+",
                            ],
                        },
                        "yaxis": {
                            "title": "MPGe per 3-min chunk (miles / 33.705 kWh)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "boxmode": "group",
                        "boxgroupgap": 0.1,
                        "boxgap": 0.15,
                        "legend": {"orientation": "h", "y": -0.25, "x": 0.05},
                        "margin": {"l": 50, "r": 20, "t": 40, "b": 60},
                    },
                    "config": {"displayModeBar": False},
                    "entities": [
                        {
                            "name": "Uphill (+)",
                            "type": "box",
                            "boxpoints": "all",
                            "jitter": 0.35,
                            "pointpos": 0,
                            "boxmean": True,
                            "marker": {
                                "symbol": "cross",
                                "size": 6,
                                "color": "#FF9800",
                                "opacity": 0.8,
                            },
                            "line": {"color": "#FF9800", "width": 1.5},
                            "fillcolor": "rgba(255, 152, 0, 0.25)",
                            "hovertemplate": "<b>Uphill chunk (+)</b><br>Speed Range: %{x} mph<br>MPGe: %{y:.1f} MPGe (%{customdata[0]:.2f} mi/kWh)<br>Avg Speed: %{customdata[1]:.1f} mph<br>Distance: %{customdata[2]:.2f} mi (%{customdata[3]:.0f}s)<br>Elevation Δh: %{customdata[4]:+.0f} ft<br>Temp: %{customdata[5]:.1f}°F<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft >= 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => s.speed_bin); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft >= 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => s.mpge || Math.round((s.efficiency_mi_kwh || 0) * 33.705 * 10) / 10); "
                                "})()"
                            ),
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft >= 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => [s.efficiency_mi_kwh, s.avg_speed_mph, s.distance_miles, s.duration_seconds, s.elevation_change_ft, s.temp_f || 70]); "
                                "})()"
                            ),
                        },
                        {
                            "name": "Downhill (o)",
                            "type": "box",
                            "boxpoints": "all",
                            "jitter": 0.35,
                            "pointpos": 0,
                            "boxmean": True,
                            "marker": {
                                "symbol": "circle",
                                "size": 6,
                                "color": "#2196F3",
                                "opacity": 0.8,
                            },
                            "line": {"color": "#2196F3", "width": 1.5},
                            "fillcolor": "rgba(33, 150, 243, 0.25)",
                            "hovertemplate": "<b>Downhill chunk (o)</b><br>Speed Range: %{x} mph<br>MPGe: %{y:.1f} MPGe (%{customdata[0]:.2f} mi/kWh)<br>Avg Speed: %{customdata[1]:.1f} mph<br>Distance: %{customdata[2]:.2f} mi (%{customdata[3]:.0f}s)<br>Elevation Δh: %{customdata[4]:+.0f} ft<br>Temp: %{customdata[5]:.1f}°F<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft < 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => s.speed_bin); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft < 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => s.mpge || Math.round((s.efficiency_mi_kwh || 0) * 33.705 * 10) / 10); "
                                "})()"
                            ),
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const order = ['0-9', '10-19', '20-29', '30-39', '40-49', '50-59', '60-69', '70-79', '80+']; "
                                f"const chunks = (window.__rivianAnalytics?.['{vin}']?.chunks || []).filter(s => s.elevation_change_ft < 0).sort((a, b) => order.indexOf(a.speed_bin) - order.indexOf(b.speed_bin)); "
                                "return chunks.map(s => [s.efficiency_mi_kwh, s.avg_speed_mph, s.distance_miles, s.duration_seconds, s.elevation_change_ft, s.temp_f || 70]); "
                                "})()"
                            ),
                        },
                    ],
                },
            ),
            # Section 7: Vampire Drain vs. Time Idle (Parked Phantom Drain Analysis)
            wrap_series_card(
                vin,
                ["vampire"],
                {
                    "type": "custom:plotly-graph",
                    "raw_plotly_config": True,
                    "title": "Vampire Drain vs. Time Idle",
                    "layout": {
                        "xaxis": {
                            "title": "Parked Idle Time (hours)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "yaxis": {
                            "title": "Vampire Drain (kWh)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "margin": {"l": 50, "r": 20, "t": 40, "b": 60},
                    },
                    "config": {"displayModeBar": False},
                    "entities": [
                        {
                            "name": "Parked Drain Event",
                            "type": "scatter",
                            "mode": "markers",
                            "marker": {
                                "size": 10,
                                "color": (
                                    f"$ex (function() {{ "
                                    f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                    "return events.map(e => e.avg_temp_f ?? 70); "
                                    "})()"
                                ),
                                "colorscale": "Bluered",
                                "showscale": True,
                                "cauto": True,
                                "colorbar": {
                                    "title": "Avg Temp (°F)",
                                    "thickness": 14,
                                    "len": 0.85,
                                    "x": 1.02,
                                },
                                "line": {"width": 1, "color": "#ffffff"},
                                "opacity": 0.9,
                            },
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                "return events.map(e => [e.drain_soc, e.rate_pct_per_day, e.avg_watts, e.avg_temp_f ?? 70, e.start_time ? e.start_time.substring(5, 16).replace('T', ' ') : '', e.end_time ? e.end_time.substring(5, 16).replace('T', ' ') : '']); "
                                "})()"
                            ),
                            "hovertemplate": "<b>Parked Vampire Drain</b><br>Idle Time: %{x:.1f} hrs<br>Drain: %{y:.2f} kWh (%{customdata[0]:.1f}%)<br>Rate: %{customdata[1]:.1f}%/day (~%{customdata[2]:.0f} W)<br>Avg Ambient Temp: %{customdata[3]:.1f}°F<br>Window: %{customdata[4]} to %{customdata[5]}<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                "return events.map(e => e.idle_hours); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                "return events.map(e => e.drain_kwh); "
                                "})()"
                            ),
                        }
                    ],
                },
            ),
            # Section 8: Vampire Drain Rate vs. Ambient Temperature (Dots Sized by Parked Idle Time)
            wrap_series_card(
                vin,
                ["vampire"],
                {
                    "type": "custom:plotly-graph",
                    "raw_plotly_config": True,
                    "title": "Vampire Drain Rate vs. Ambient Temperature",
                    "layout": {
                        "xaxis": {
                            "title": "Ambient Temperature (°F)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "yaxis": {
                            "title": "Drain Rate (% SoC / day)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "margin": {"l": 50, "r": 20, "t": 40, "b": 60},
                    },
                    "config": {"displayModeBar": False},
                    "entities": [
                        {
                            "name": "Parked Drain Rate",
                            "type": "scatter",
                            "mode": "markers",
                            "marker": {
                                "size": (
                                    f"$ex (function() {{ "
                                    f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                    "return events.map(e => Math.max(7, Math.min(32, Math.round(6 + Math.sqrt(e.idle_hours || 0) * 4)))); "
                                    "})()"
                                ),
                                "color": (
                                    f"$ex (function() {{ "
                                    f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                    "return events.map(e => e.avg_watts || 0); "
                                    "})()"
                                ),
                                "colorscale": "Viridis",
                                "showscale": True,
                                "cauto": True,
                                "colorbar": {
                                    "title": "Avg Watts (W)",
                                    "thickness": 14,
                                    "len": 0.85,
                                    "x": 1.02,
                                },
                                "line": {"width": 1, "color": "#ffffff"},
                                "opacity": 0.9,
                            },
                            "customdata": (
                                f"$ex (function() {{ "
                                f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                "return events.map(e => [e.idle_hours, e.drain_kwh, e.drain_soc, e.avg_watts, e.start_time ? e.start_time.substring(5, 16).replace('T', ' ') : '', e.end_time ? e.end_time.substring(5, 16).replace('T', ' ') : '']); "
                                "})()"
                            ),
                            "hovertemplate": "<b>Parked Vampire Drain Rate</b><br>Ambient Temp: %{x:.1f}°F<br>Loss Rate: %{y:.2f}%/day<br>Avg Continuous Load: %{customdata[3]:.0f} W<br>Total Drain: %{customdata[1]:.2f} kWh (%{customdata[2]:.1f}%)<br>Parked Idle Time: %{customdata[0]:.1f} hrs<br>Window: %{customdata[4]} to %{customdata[5]}<extra></extra>",
                            "x": (
                                f"$ex (function() {{ "
                                f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                "return events.map(e => e.avg_temp_f ?? 70); "
                                "})()"
                            ),
                            "y": (
                                f"$ex (function() {{ "
                                f"const events = (window.__rivianAnalytics?.['{vin}']?.vampire || []).filter(e => (e.drain_kwh || 0) > 0); "
                                "return events.map(e => e.rate_pct_per_day); "
                                "})()"
                            ),
                        }
                    ],
                },
            ),
            # Section 9: DC Fast Charging Curves (Power vs. Battery SoC)
            wrap_series_card(
                vin,
                ["dcfc"],
                {
                    "type": "custom:plotly-graph",
                    "raw_plotly_config": True,
                    "title": "DC Fast Charging Curves (Power vs. Battery SoC)",
                    "layout": {
                        "xaxis": {
                            "title": "Battery State of Charge (%)",
                            "range": [0, 100],
                            "type": "linear",
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "yaxis": {
                            "title": "Charging Power (kW)",
                            "type": "linear",
                            "autorange": True,
                            "gridcolor": "#444444",
                            "zeroline": False,
                        },
                        "legend": {"orientation": "h", "y": -0.25, "x": 0.05},
                        "margin": {"l": 50, "r": 20, "t": 40, "b": 60},
                    },
                    "config": {"displayModeBar": False},
                    "entities": (
                        [
                            {
                                "name": (
                                    f"$ex (function() {{ "
                                    f"const sessions = (window.__rivianAnalytics?.['{vin}']?.dcfc || []).slice(-10); "
                                    f"if ({idx} >= sessions.length) return ''; "
                                    f"const s = sessions[{idx}]; "
                                    f"const label = s.start_time ? s.start_time.substring(5, 16).replace('T', ' ') : 'Session {idx + 1}'; "
                                    f"return label + ' (Peak: ' + Math.round(s.max_power_kw || 0) + ' kW)'; "
                                    f"}})()"
                                ),
                                "showlegend": (
                                    f"$ex (function() {{ "
                                    f"const sessions = (window.__rivianAnalytics?.['{vin}']?.dcfc || []).slice(-10); "
                                    f"return {idx} < sessions.length; "
                                    f"}})()"
                                ),
                                "type": "scatter",
                                "mode": "lines+markers",
                                "line": {"color": color, "width": 2},
                                "marker": {"size": 5, "color": color},
                                "customdata": (
                                    f"$ex (function() {{ "
                                    f"const sessions = (window.__rivianAnalytics?.['{vin}']?.dcfc || []).slice(-10); "
                                    f"if ({idx} >= sessions.length) return []; "
                                    f"const s = sessions[{idx}]; "
                                    f"const label = s.start_time ? s.start_time.substring(5, 16).replace('T', ' ') : 'Session {idx + 1}'; "
                                    f"return (s.samples || []).map(pt => [label, s.energy_added_kwh || 0, s.max_power_kw || 0]); "
                                    f"}})()"
                                ),
                                "hovertemplate": (
                                    "<b>%{customdata[0]}</b><br>"
                                    "SoC: %{x:.1f}%<br>"
                                    "Power: %{y:.1f} kW<br>"
                                    "Energy Added: +%{customdata[1]:.1f} kWh<br>"
                                    "Session Peak: %{customdata[2]:.0f} kW<extra></extra>"
                                ),
                                "x": (
                                    f"$ex (function() {{ "
                                    f"const sessions = (window.__rivianAnalytics?.['{vin}']?.dcfc || []).slice(-10); "
                                    f"if ({idx} >= sessions.length) return []; "
                                    f"return (sessions[{idx}].samples || []).map(pt => pt.soc); "
                                    f"}})()"
                                ),
                                "y": (
                                    f"$ex (function() {{ "
                                    f"const sessions = (window.__rivianAnalytics?.['{vin}']?.dcfc || []).slice(-10); "
                                    f"if ({idx} >= sessions.length) return []; "
                                    f"return (sessions[{idx}].samples || []).map(pt => pt.power_kw); "
                                    f"}})()"
                                ),
                            }
                            for idx, color in enumerate(DCFC_SESSION_COLORS)
                        ]
                        + [
                            {
                                "name": "Average DCFC Curve",
                                "type": "scatter",
                                "mode": "lines+markers",
                                "line": {"color": "#FFFFFF", "width": 3.5},
                                "marker": {
                                    "size": 6,
                                    "color": "#FFFFFF",
                                    "symbol": "circle",
                                },
                                "customdata": (
                                    f"$ex (function() {{ "
                                    f"const sessions = (window.__rivianAnalytics?.['{vin}']?.dcfc || []); "
                                    "if (!sessions || sessions.length === 0) return []; "
                                    "const buckets = {}; "
                                    "sessions.forEach(s => { (s.samples || []).forEach(pt => { const b = Math.round(pt.soc); if (!buckets[b]) buckets[b] = []; buckets[b].push(pt.power_kw); }); }); "
                                    "const socs = Object.keys(buckets).map(Number).sort((a, b) => a - b); "
                                    "return socs.map(soc => [buckets[soc].length, sessions.length + ' sessions']); "
                                    "})()"
                                ),
                                "hovertemplate": "<b>Average DCFC Curve</b><br>SoC: %{x}%<br>Average Power: %{y:.1f} kW<br>Observed: %{customdata[0]} samples across %{customdata[1]}<extra></extra>",
                                "x": (
                                    f"$ex (function() {{ "
                                    f"const sessions = (window.__rivianAnalytics?.['{vin}']?.dcfc || []); "
                                    "if (!sessions || sessions.length === 0) return []; "
                                    "const buckets = {}; "
                                    "sessions.forEach(s => { (s.samples || []).forEach(pt => { const b = Math.round(pt.soc); if (!buckets[b]) buckets[b] = []; buckets[b].push(pt.power_kw); }); }); "
                                    "const socs = Object.keys(buckets).map(Number).sort((a, b) => a - b); "
                                    "return socs; "
                                    "})()"
                                ),
                                "y": (
                                    f"$ex (function() {{ "
                                    f"const sessions = (window.__rivianAnalytics?.['{vin}']?.dcfc || []); "
                                    "if (!sessions || sessions.length === 0) return []; "
                                    "const buckets = {}; "
                                    "sessions.forEach(s => { (s.samples || []).forEach(pt => { const b = Math.round(pt.soc); if (!buckets[b]) buckets[b] = []; buckets[b].push(pt.power_kw); }); }); "
                                    "const socs = Object.keys(buckets).map(Number).sort((a, b) => a - b); "
                                    "return socs.map(soc => { const vals = buckets[soc]; return Math.round((vals.reduce((a, b) => a + b, 0) / vals.length) * 10) / 10; }); "
                                    "})()"
                                ),
                            },
                            {
                                "name": (
                                    f"$ex (function() {{ "
                                    f"const cap = parseFloat(hass.states['{battery_cap_entity}']?.state || hass.states['{battery_cap_fallback}']?.state || 135.0); "
                                    "if (cap > 139.0) return 'R1 Max Pack (149 kWh Ref)'; "
                                    "if (cap < 115.0) return 'R1 Standard Pack (106 kWh Ref)'; "
                                    "return 'R1 Large Pack (135 kWh Ref)'; "
                                    f"}})()"
                                ),
                                "type": "scatter",
                                "mode": "lines",
                                "line": {
                                    "color": "rgba(255, 255, 255, 0.3)",
                                    "width": 1.5,
                                    "dash": "dot",
                                },
                                "hovertemplate": (
                                    f"$ex (function() {{ "
                                    f"const cap = parseFloat(hass.states['{battery_cap_entity}']?.state || hass.states['{battery_cap_fallback}']?.state || 135.0); "
                                    "const label = cap > 139.0 ? 'Max Pack (149 kWh Ref)' : (cap < 115.0 ? 'Standard Pack (106 kWh Ref)' : 'Large Pack (135 kWh Ref)'); "
                                    "return '<b>Rivian ' + label + '</b><br>SoC: %{x}%<br>Power: %{y} kW<extra></extra>'; "
                                    f"}})()"
                                ),
                                "x": [
                                    10,
                                    15,
                                    20,
                                    25,
                                    30,
                                    35,
                                    40,
                                    45,
                                    50,
                                    55,
                                    60,
                                    65,
                                    70,
                                    75,
                                    80,
                                    85,
                                    90,
                                ],
                                "y": (
                                    f"$ex (function() {{ "
                                    f"const cap = parseFloat(hass.states['{battery_cap_entity}']?.state || hass.states['{battery_cap_fallback}']?.state || 135.0); "
                                    "if (cap > 139.0) return [220, 220, 218, 215, 210, 198, 185, 172, 160, 146, 132, 118, 104, 88, 70, 50, 32]; "
                                    "if (cap < 115.0) return [205, 205, 200, 195, 185, 170, 155, 140, 125, 110, 95, 82, 70, 58, 45, 32, 20]; "
                                    "return [215, 215, 212, 208, 200, 185, 170, 155, 145, 130, 118, 105, 92, 78, 62, 45, 28]; "
                                    f"}})()"
                                ),
                            },
                        ]
                    ),
                },
            ),
            # Section 10: Detailed Statistics Grid
            {
                "type": "grid",
                "title": "Drive Telemetry",
                "columns": 3,
                "square": False,
                "cards": [
                    {
                        "type": "custom:mushroom-entity-card",
                        "entity": last_dist_entity,
                        "name": "Last Distance",
                        "icon": "mdi:map-marker-distance",
                        "icon_color": "blue",
                    },
                    {
                        "type": "custom:mushroom-entity-card",
                        "entity": last_mpge_entity,
                        "name": "Last MPGe",
                        "icon": "mdi:gas-station-off",
                        "icon_color": "teal",
                    },
                    {
                        "type": "custom:mushroom-entity-card",
                        "entity": status_entity,
                        "name": "Drive Status",
                        "icon": "mdi:car-electric",
                        "icon_color": "green",
                    },
                    {
                        "type": "custom:mushroom-entity-card",
                        "entity": eff_30d_entity,
                        "name": "30-Day Efficiency",
                        "icon": "mdi:calendar-month",
                        "icon_color": "green",
                    },
                    {
                        "type": "custom:mushroom-entity-card",
                        "entity": mpge_30d_entity,
                        "name": "30-Day MPGe",
                        "icon": "mdi:gauge",
                        "icon_color": "teal",
                    },
                    {
                        "type": "custom:mushroom-entity-card",
                        "entity": eff_all_entity,
                        "name": "All-Time Efficiency",
                        "icon": "mdi:all-inclusive",
                        "icon_color": "purple",
                    },
                ],
            },
        ],
    }


# Logical name -> (domain, unique_id key). The full unique_id looked up in the
# entity registry is f"{vin}-{key}". Kept as a flat, easy-to-extend table
# rather than guessing entity ids by slugifying names onto a discovered
# sensor prefix -- that guessing is exactly the bug this table replaces (core
# entity naming varies per install; the drive/analytics sensors' shared
# `entity_prefix` scheme is the one case where guessing is still reliable).
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
    """Map every discovered VIN to its "<year> <model>" label, if known.

    Demo vehicles (see ``demo.py``) contribute their model name (e.g. "R2").
    """
    models: dict[str, str] = {}
    for demo in get_demo_vehicles(hass):
        models[demo["vin"]] = demo.get("model", "")
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


def _build_efficiency_cards(
    vehicle_name: str,
    entity_prefix: str,
    vin: str,
    days: int = DEFAULT_CHART_WINDOW_DAYS,
    entities_map: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Build the Efficiency tab's cards: the scatter/box charts plus the stats grid.

    Requires a VIN (every chart here is a bulk-series WebSocket card); without
    one there is nothing meaningful to show, so this returns an empty list.
    These charts read the drive/analytics sensors' shared `entity_prefix`,
    which (unlike core entities) is reliably discovered, so `entities_map` is
    accepted for a consistent tab-builder signature but unused here.
    """
    if not vin:
        return []
    cards = _build_vehicle_analytics_view(vehicle_name, entity_prefix, vin, days)[
        "cards"
    ]
    # cards[1:6] = temp-vs-efficiency, distance-vs-efficiency, speed bins,
    # and the two box plots; cards[9] = the Detailed Statistics Grid.
    # The telemetry grid reads the vehicle's entities, which a demo vehicle
    # (no entity prefix) doesn't have.
    return [*cards[1:6], *([cards[9]] if entity_prefix else [])]


VEHICLE_BAR_CARD: Final = "custom:rivian-vehicle-bar-card"


def _vehicle_blocks(
    tab_builder: Callable[..., list[dict[str, Any]]],
    vehicles: list[tuple[str, str, str]],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Apply a per-vehicle tab-card builder across one or more vehicles.

    With a single vehicle, its cards are returned directly. With several,
    every vehicle's cards are stacked under a heading with its name: no
    picker and no conditionals (the vehicle bar at the top of the tab selects
    which vehicles are *summarized*; the per-vehicle Plotly blocks remain
    until the charts themselves go multi-vehicle).
    """
    if not vehicles:
        return []
    if len(vehicles) == 1:
        name, prefix, vin = vehicles[0]
        return tab_builder(name, prefix, vin, **kwargs)
    out: list[dict[str, Any]] = []
    for name, prefix, vin in vehicles:
        out.append({"type": "heading", "heading": name})
        out.extend(tab_builder(name, prefix, vin, **kwargs))
    return out


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


def _build_charging_view(vehicles: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Build the panel-mode Charging tab: one charging & battery card."""
    return _build_panel_view(
        vehicles,
        title="Charging",
        path="charging",
        icon="mdi:ev-station",
        card_type="custom:rivian-charging-card",
    )


def _build_efficiency_view(vehicles: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Build the panel-mode Efficiency tab: one efficiency card."""
    return _build_panel_view(
        vehicles,
        title="Efficiency",
        path="efficiency",
        icon="mdi:chart-scatter-plot",
        card_type="custom:rivian-efficiency-card",
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


def _collect_chart_cards(node: Any) -> list[dict[str, Any]]:
    """Recursively collect chart cards (CHART_CARD_TYPES) from a nested card tree.

    Descends into "cards" (grid/vertical-stack/lists) and "card" (conditional)
    so charts nested behind a vehicle-picker conditional are still found. A
    matched `custom:rivian-series-card` wrapper is not descended into any
    further: its own "card" key holds the wrapped plotly-graph spec, which is
    part of that same chart, not an independent second one.
    """
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type in CHART_CARD_TYPES:
            found.append(node)
            if node_type == "custom:rivian-series-card":
                return found
        for key in ("cards", "card"):
            if key in node:
                found.extend(_collect_chart_cards(node[key]))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_chart_cards(item))
    return found


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
                    {
                        "type": "tile",
                        "entity": last_eff_entity,
                        "name": "Last Drive Efficiency",
                        "icon": "mdi:leaf",
                        "color": "green",
                    },
                    {
                        "type": "tile",
                        "entity": last_dist_entity,
                        "name": "Last Drive Distance",
                        "icon": "mdi:map-marker-distance",
                        "color": "blue",
                    },
                    {
                        "type": "tile",
                        "entity": last_mpge_entity,
                        "name": "Last Drive MPGe",
                        "icon": "mdi:gas-station-off",
                        "color": "teal",
                    },
                    {
                        "type": "tile",
                        "entity": status_entity,
                        "name": "Drive Status",
                        "icon": "mdi:car-electric",
                        "color": "amber",
                    },
                    {
                        "type": "tile",
                        "entity": eff_30d_entity,
                        "name": "30-Day Efficiency",
                        "icon": "mdi:calendar-month",
                        "color": "green",
                    },
                    {
                        "type": "tile",
                        "entity": mpge_30d_entity,
                        "name": "30-Day MPGe",
                        "icon": "mdi:gauge",
                        "color": "teal",
                    },
                    {
                        "type": "tile",
                        "entity": eff_all_entity,
                        "name": "All-Time Efficiency",
                        "icon": "mdi:all-inclusive",
                        "color": "purple",
                    },
                    {
                        "type": "tile",
                        "entity": mpge_all_entity,
                        "name": "All-Time MPGe",
                        "icon": "mdi:gas-station-off",
                        "color": "indigo",
                    },
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


async def async_discover_vehicle_prefixes(
    hass: HomeAssistant,
) -> list[tuple[str, str, str, str]]:
    """Discover configured Rivian vehicle names, entity ID prefixes, VINs, and entry ids.

    The fourth tuple element is the owning config entry's `entry_id`, used to
    resolve that entry's "Dashboard vehicle" select entity (see `select.py`)
    when more than one vehicle is discovered; it's empty when discovered via
    the entity-registry fallback below, which has no entry context.
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

    # Demo vehicles (no entities, no config entry of their own) join the
    # real ones, borrowing the first real vehicle's entry id so the picker
    # select resolves for them too.
    known_vins = {vin for (_n, _p, vin, _e) in results}
    entry_id = next((eid for (_n, _p, _v, eid) in results if eid), "")
    for demo in get_demo_vehicles(hass):
        if demo["vin"] not in known_vins:
            results.append((demo["name"], "", demo["vin"], entry_id))

    return results


def _resolve_chart_window_days(hass: HomeAssistant) -> int:
    """Return the first Rivian config entry's configured chart history window.

    Falls back to the default on any lookup failure (e.g. in tests that stub
    `hass` without a real config-entries manager) since this is purely a
    cosmetic default for freshly generated charts, never worth failing
    dashboard generation over.
    """
    try:
        entries = list(hass.config_entries.async_entries(DOMAIN))
        if entries:
            return int(
                entries[0].options.get(
                    CONF_CHART_WINDOW_DAYS, DEFAULT_CHART_WINDOW_DAYS
                )
            )
    except Exception as err:  # noqa: BLE001 - never fail dashboard generation over this
        _LOGGER.debug("Could not resolve chart_window_days option: %s", err)
    return DEFAULT_CHART_WINDOW_DAYS


async def async_create_efficiency_dashboard(
    hass: HomeAssistant,
    title: str = DEFAULT_TITLE,
    icon: str = DEFAULT_ICON,
    url_path: str = DEFAULT_URL_PATH,
) -> bool:
    """Create or update the turnkey, tabbed Rivian dashboard in Home Assistant.

    Views/tabs are generated in order: Overview, Drives (panel), Places
    (panel), Routes (panel), Charging (panel) and Efficiency (panel). The Overview tab is a
    single `rivian-overview-card` listing every vehicle (see
    `_build_overview_view`). Drives, Places and Routes each hold ONE card
    that follows the shared vehicle selection. Charging is a panel view holding the one
    `rivian-charging-card` and Efficiency the one `rivian-efficiency-card` (the
    vehicle bar renders inside each). The Plotly chart blocks (see
    `_vehicle_blocks`) now only feed the optional dashboard-automobiles
    injection. There is no picker or
    conditional card anywhere; the "Dashboard vehicle" select is deprecated.
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
    chart_window_days = _resolve_chart_window_days(hass)

    entities_by_vin: dict[str, dict[str, str]] = {}
    for _name, _prefix, vin in vehicles:
        if vin:
            entities_by_vin[vin] = await _async_resolve_vehicle_entities(hass, vin)
    vehicle_models = _collect_vehicle_models(hass)

    overview_view = _build_overview_view(
        vehicles_with_entry, entities_by_vin, vehicle_models, url_path
    )
    has_vin = _has_vin(vehicles)
    bar_cards: list[dict[str, Any]] = [{"type": VEHICLE_BAR_CARD}] if has_vin else []
    # The Rivian dashboard's Efficiency tab is one panel card; the Plotly stacks
    # are only still built for the optional dashboard-automobiles injection below.
    efficiency_cards = [
        *bar_cards,
        *_vehicle_blocks(
            _build_efficiency_cards,
            vehicles,
            days=chart_window_days,
            entities_map=entities_by_vin,
        ),
    ]
    drives_view = _build_drives_view(vehicles)
    places_view = _build_places_view(vehicles)
    routes_view = _build_routes_view(vehicles)
    charging_view = _build_charging_view(vehicles)
    efficiency_view = _build_efficiency_view(vehicles)

    views: list[dict[str, Any]] = [
        overview_view,
        drives_view,
        routes_view,
        places_view,
        charging_view,
        efficiency_view,
        # No Vehicle tab for now: the user found the status/controls view of
        # little value; a redesigned one is planned for a later phase.
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

    # 3. If dashboard_automobiles exists, inject the Efficiency view and
    # Section 2 Plotly cards. Match wrapped and bare charts: dashboards
    # generated by earlier versions hold bare plotly-graph cards, which must
    # be replaced rather than left behind.
    chart_cards = _collect_chart_cards(efficiency_cards)
    try:
        auto_config = await _async_load_dashboard_config(
            hass, AUTOMOBILES_URL_PATH, AUTOMOBILES_STORAGE_KEY
        )
        # With no charts (no VIN resolved) there is nothing to inject, and carrying
        # on would strip the existing charts out of the user's dashboard.
        if chart_cards and auto_config:
            auto_views = auto_config.get("views", [])

            # Update View 0 Section 2 cards if sections exist
            if (
                auto_views
                and "sections" in auto_views[0]
                and len(auto_views[0]["sections"]) >= 3
            ):
                sec2 = auto_views[0]["sections"][2]
                sec2_cards = sec2.get("cards", [])
                user_cards = [
                    c for c in sec2_cards if c.get("type") not in CHART_CARD_TYPES
                ]
                sec2["cards"] = user_cards + chart_cards

            # Update or append the 'Efficiency & Analytics' tab (path "efficiency")
            has_eff = False
            for v in auto_views:
                if v.get("path") == "efficiency":
                    v["title"] = "Efficiency & Analytics"
                    v["icon"] = "mdi:chart-scatter-plot"
                    v["cards"] = efficiency_cards
                    has_eff = True
                    break
            if not has_eff:
                auto_views.append(
                    {
                        "title": "Efficiency & Analytics",
                        "path": "efficiency",
                        "icon": "mdi:chart-scatter-plot",
                        "cards": efficiency_cards,
                    }
                )

            await _async_save_dashboard_config(
                hass, AUTOMOBILES_URL_PATH, AUTOMOBILES_STORAGE_KEY, auto_config
            )
            _LOGGER.info(
                "Successfully synced efficiency view into dashboard-automobiles"
            )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not auto-inject into dashboard_automobiles: %s", err)

    return True
