"""Rivian (Unofficial)"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Final

from rivian import Rivian
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store

from .analytics_db import AnalyticsDatabase
from .config_flow import (
    CONF_ANALYTICS_RETENTION_DAYS,
    CONF_PLACE_GEOCODING,
    CONF_TRACK_FULL_DETAIL_DAYS,
    CONF_TRACK_RETENTION_DAYS,
    DEFAULT_ANALYTICS_RETENTION_DAYS,
    DEFAULT_PLACE_GEOCODING,
    DEFAULT_TRACK_FULL_DETAIL_DAYS,
    DEFAULT_TRACK_RETENTION_DAYS,
)
from .const import (
    ATTR_ANALYTICS_DB,
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_DEMO_STORES,
    ATTR_DEMO_VEHICLES,
    ATTR_DRIVE_STORE,
    ATTR_DRIVE_TRACKER,
    ATTR_USER,
    ATTR_VEHICLE,
    ATTR_WALLBOX,
    CONF_VEHICLE_CONTROL,
    DASHBOARD_SCHEMA_VERSION,
    DOMAIN,
    ISSUE_URL,
    RIVIAN_ANALYTICS_UPDATED_EVENT,
    VERSION,
)

try:
    from homeassistant.components.frontend import add_extra_js_url
except ImportError:

    def add_extra_js_url(*args: Any, **kwargs: Any) -> None:  # type: ignore[misc]
        pass


try:
    from homeassistant.components.http import StaticPathConfig
except ImportError:

    class StaticPathConfig:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass


from . import charging_history
from .coordinator import UserCoordinator, VehicleCoordinator, WallboxCoordinator
from .dashboard_generator import (
    DEFAULT_ICON,
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_create_efficiency_dashboard,
)
from .demo import (
    async_install_demo,
    async_remove_demo,
    async_setup_demo_registry,
    async_teardown_demo_registry,
    is_demo_vin,
)
from .drive_storage import DriveStore, read_zone_states
from .drive_tracker import DriveEvent, DriveTracker
from .helpers import get_rivian_api_from_entry
from .history_backfill import async_backfill_from_recorder
from .image import async_apply_vehicle_picture
from .statistics import async_update_statistics
from .vehicle_picture import async_picture_from_url
from .websocket_api import async_register_websocket_api

SERVICE_BACKFILL_DRIVE_HISTORY = "backfill_drive_history"
SERVICE_CREATE_EFFICIENCY_DASHBOARD = "create_efficiency_dashboard"
SERVICE_SET_VEHICLE_PICTURE = "set_vehicle_picture"
SERVICE_REBUILD_HEAT_MAP = "rebuild_heat_map"
SERVICE_RECOMPUTE_DRIVE_STATS = "recompute_drive_stats"
SERVICE_FIT_ENERGY_MODEL = "fit_energy_model"
SERVICE_BACKFILL_WEATHER = "backfill_weather"
SERVICE_SNAP_ROUTE_GAPS = "snap_route_gaps"
SERVICE_REBUILD_PLACES = "rebuild_places"
SERVICE_REBUILD_ROUTES = "rebuild_routes"
SERVICE_IMPORT_CHARGING_HISTORY = "import_charging_history"
SERVICE_INFER_CHARGING_SESSIONS = "infer_charging_sessions"
SERVICE_CREATE_DEMO_DATA = "create_demo_data"
SERVICE_DELETE_DEMO_DATA = "delete_demo_data"

# Special (non-config-entry) keys stored directly under hass.data[DOMAIN].
_ANALYTICS_DB_LOCK_KEY: Final = "_analytics_db_lock"
_ZONE_LISTENER_REGISTERED_KEY: Final = "_zone_listener_registered"
_ZONE_LISTENER_REMOVE_KEY: Final = "_zone_listener_remove"
_DASHBOARD_AUTOCREATE_KEY: Final = "_dashboard_autocreate_claimed"
_SPECIAL_DOMAIN_DATA_KEYS: Final = frozenset(
    {
        ATTR_ANALYTICS_DB,
        ATTR_DEMO_STORES,
        ATTR_DEMO_VEHICLES,
        _ANALYTICS_DB_LOCK_KEY,
        "_ws_api_registered",
        _ZONE_LISTENER_REGISTERED_KEY,
        _ZONE_LISTENER_REMOVE_KEY,
        _DASHBOARD_AUTOCREATE_KEY,
    }
)

RETENTION_PRUNE_INITIAL_DELAY_SECONDS: Final = 30
RETENTION_PRUNE_INTERVAL: Final = timedelta(hours=24)
# A zone.* state_changed event schedules a zone resync after this long of
# quiet, so editing several zones in a row (or a batch zone import) triggers
# one resync, not one per zone.
ZONE_SYNC_DEBOUNCE_SECONDS: Final = 10

# Bundled frontend cards registered as Lovelace resources by
# _async_register_frontend.
_BUNDLED_MODULES: Final = (
    "plotly-graph-card.js",
    "mushroom.js",
    "rivian-series-card.js",
    "rivian-drive-explorer-card.js",
    "rivian-overview-card.js",
    "rivian-places-card.js",
    "rivian-routes-card.js",
    "rivian-charging-sessions-card.js",
    "rivian-charging-card.js",
    "rivian-efficiency-card.js",
    # Shared by the cards above (imported dynamically) and a tiny card for
    # tabs without a panel header. Skipped at registration while not on disk.
    "rivian-vehicle-bar.js",
    "rivian-vehicle-bar-card.js",
)

BACKFILL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
        vol.Optional("days"): vol.Coerce(int),
        vol.Optional("dry_run", default=True): cv.boolean,
        vol.Optional("db_path"): cv.string,
        vol.Optional("tracks", default=True): cv.boolean,
    }
)

CREATE_DASHBOARD_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("title", default=DEFAULT_TITLE): cv.string,
        vol.Optional("icon", default=DEFAULT_ICON): cv.string,
        vol.Optional("url_path", default=DEFAULT_URL_PATH): cv.string,
    }
)

SET_VEHICLE_PICTURE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
        vol.Required("url"): cv.url,
    }
)

REBUILD_HEAT_MAP_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

RECOMPUTE_DRIVE_STATS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

BACKFILL_WEATHER_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
        vol.Optional("days", default=365): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=365)
        ),
    }
)

FIT_ENERGY_MODEL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

IMPORT_CHARGING_HISTORY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

INFER_CHARGING_SESSIONS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

SNAP_ROUTE_GAPS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

REBUILD_PLACES_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

REBUILD_ROUTES_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

CREATE_DEMO_DATA_SERVICE_SCHEMA = vol.Schema({})

DELETE_DEMO_DATA_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): cv.string,
    }
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.DEVICE_TRACKER,
    Platform.IMAGE,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.UPDATE,
]


def _iter_entry_datas(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the per-config-entry data dicts stored under hass.data[DOMAIN].

    hass.data[DOMAIN] also holds special, non-entry keys (the shared
    AnalyticsDatabase, its setup lock, and the websocket API registration
    flag) alongside the entry_id-keyed dicts, so callers must filter those
    out before assuming every value is a config entry's data dict.
    """
    return [
        value
        for key, value in hass.data.get(DOMAIN, {}).items()
        if key not in _SPECIAL_DOMAIN_DATA_KEYS and isinstance(value, dict)
    ]


async def _async_get_or_create_analytics_db(hass: HomeAssistant) -> AnalyticsDatabase:
    """Return the single shared AnalyticsDatabase for this HA instance.

    Created once per Home Assistant instance (not per config entry); a second
    vehicle/account config entry reuses the already-opened instance. Guarded
    by a lock so two config entries setting up concurrently can't both open
    the database.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock: asyncio.Lock = domain_data.setdefault(_ANALYTICS_DB_LOCK_KEY, asyncio.Lock())
    async with lock:
        db: AnalyticsDatabase | None = domain_data.get(ATTR_ANALYTICS_DB)
        if db is None:
            db = AnalyticsDatabase(hass)
            await hass.async_add_executor_job(db.setup)
            domain_data[ATTR_ANALYTICS_DB] = db
    return db


def _async_check_analytics_db_issues(
    hass: HomeAssistant, db: AnalyticsDatabase
) -> None:
    """Raise (or clear) repair issues reflecting the shared analytics DB's health."""
    try:
        if db.was_repaired:
            async_create_issue(
                hass,
                DOMAIN,
                "analytics_db_repaired",
                is_fixable=False,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="analytics_db_repaired",
            )
        else:
            async_delete_issue(hass, DOMAIN, "analytics_db_repaired")

        if db.read_only:
            async_create_issue(
                hass,
                DOMAIN,
                "analytics_db_read_only",
                is_fixable=False,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="analytics_db_read_only",
            )
        else:
            async_delete_issue(hass, DOMAIN, "analytics_db_read_only")
    except Exception as err:  # noqa: BLE001 - repair issues must never break setup
        _LOGGER.debug("Could not update analytics DB repair issues: %s", err)


_REMOVED_ATTRIBUTE_READ: Final = re.compile(
    r"recent_(drives|segments|vampire_events|dcfc_sessions)"
)


async def _async_check_dashboard_staleness(hass: HomeAssistant) -> None:
    """Raise a repair issue if any dashboard still reads the removed bulk attributes.

    Best-effort and defensive: any failure here is logged and swallowed, and
    must never fail integration setup.
    """
    try:
        dashboards_store: Store[Any] = Store(hass, 1, "lovelace_dashboards")
        dashboards_data = await dashboards_store.async_load()
        # The default Overview dashboard isn't listed in lovelace_dashboards, but
        # it's a common place to have pasted the old YAML templates.
        store_keys = ["lovelace"] + [
            f"lovelace.{item['id']}"
            for item in (dashboards_data or {}).get("items", [])
            if item.get("id")
        ]
        stale_found = False
        for key in store_keys:
            config_store: Store[Any] = Store(hass, 1, key)
            config_data = await config_store.async_load()
            if not config_data:
                continue
            dashboard_config = config_data.get("config", {})
            schema_version = dashboard_config.get("schema_version")
            if schema_version is not None:
                stale = schema_version < DASHBOARD_SCHEMA_VERSION
            else:
                # Every dashboard generated before versioning lacks schema_version,
                # so unversioned is not "not ours": it's stale if it reads the
                # bulk attributes the sensors no longer publish.
                stale = bool(
                    _REMOVED_ATTRIBUTE_READ.search(json.dumps(dashboard_config))
                )
            if stale:
                stale_found = True
                break

        if stale_found:
            async_create_issue(
                hass,
                DOMAIN,
                "dashboard_schema_stale",
                is_fixable=False,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="dashboard_schema_stale",
            )
        else:
            async_delete_issue(hass, DOMAIN, "dashboard_schema_stale")
    except Exception as err:  # noqa: BLE001 - dashboard check must never break setup
        _LOGGER.debug("Dashboard staleness check failed (non-fatal): %s", err)


# Persisted once-per-instance flag for the first-setup dashboard. A small HA
# Store rather than the analytics DB's meta table: no executor round trip, and
# it stays independent of the database's lifecycle (a wiped or rebuilt
# analytics DB must not bring back a dashboard the user deleted).
_DASHBOARD_AUTOCREATE_STORE: Final = "rivian_dashboard_autocreate"


def _lovelace_is_yaml_mode(hass: HomeAssistant) -> bool:
    """Return True when the Lovelace UI is configured in YAML mode."""
    lovelace = hass.data.get("lovelace")
    mode = getattr(lovelace, "mode", None)
    if mode is None and isinstance(lovelace, dict):
        mode = lovelace.get("mode")
    return mode == "yaml"


async def _async_dashboard_exists(hass: HomeAssistant, url_path: str) -> bool:
    """Return True if a Lovelace dashboard with ``url_path`` is registered."""
    dashboards = getattr(hass.data.get("lovelace"), "dashboards", None)
    if isinstance(dashboards, dict) and url_path in dashboards:
        return True
    data = await Store(hass, 1, "lovelace_dashboards").async_load()
    return any(
        item.get("url_path") == url_path for item in (data or {}).get("items", [])
    )


async def _async_auto_create_dashboard(hass: HomeAssistant) -> None:
    """Create the Rivian dashboard once per HA instance, on first setup.

    Skips when the persisted flag is already set. The flag is also set when the
    dashboard already exists (so deleting it later is never undone) and in YAML
    mode, where a notification explains how to add it. Never raises.
    """
    try:
        store: Store[Any] = Store(hass, 1, _DASHBOARD_AUTOCREATE_STORE)
        if (await store.async_load() or {}).get("created"):
            return

        if _lovelace_is_yaml_mode(hass):
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                hass,
                "Lovelace is in YAML mode, so the Rivian dashboard could not be "
                "created automatically. After switching Lovelace to storage "
                "mode, run **Developer tools > Actions > Create Rivian "
                "dashboard** (`rivian.create_efficiency_dashboard`), or see the "
                "integration's documentation to add the views by hand.",
                title="Rivian dashboard",
                notification_id="rivian_dashboard_yaml_mode",
            )
        elif not await _async_dashboard_exists(hass, DEFAULT_URL_PATH):
            await async_create_efficiency_dashboard(
                hass=hass,
                title=DEFAULT_TITLE,
                icon=DEFAULT_ICON,
                url_path=DEFAULT_URL_PATH,
            )
            _LOGGER.info("Created the Rivian dashboard on first setup")
        await store.async_save({"created": True})
    except Exception as err:  # noqa: BLE001 - must never break setup
        _LOGGER.warning(
            "Could not create the Rivian dashboard automatically (run the "
            "rivian.create_efficiency_dashboard action to create it): %s",
            err,
        )


def _schedule_dashboard_autocreate(hass: HomeAssistant) -> None:
    """Start the first-setup dashboard task once per instance, without blocking."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_DASHBOARD_AUTOCREATE_KEY):
        return
    domain_data[_DASHBOARD_AUTOCREATE_KEY] = True

    # @callback: HA runs a plain (non-callback) listener in a worker thread,
    # where creating a task fails ("Task was destroyed but it is pending").
    @callback
    def _start(_event: Any = None) -> None:
        hass.async_create_background_task(
            _async_auto_create_dashboard(hass), "rivian_dashboard_autocreate"
        )

    # Waits for HA to finish starting when it hasn't yet, so Lovelace and the
    # entity registry are fully populated.
    if getattr(hass, "is_running", False):
        _start()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register bundled frontend cards so they load automatically without HACS.

    Each module is registered exactly once, as a Lovelace resource with a
    cache-busting ``?v={VERSION}`` query string; a stale ``?v=`` from a
    previous version is updated in place. ``add_extra_js_url`` (which has no
    cache-busting story) is only used as a fallback for YAML-mode Lovelace,
    where there's no storage-backed resources collection to register with --
    registering both, as earlier versions did, made every browser load each
    module twice. Leaflet's files are served by the static path above but are
    imported directly by the drive-explorer card's JS, not as Lovelace
    resources.
    """
    frontend_dir = Path(__file__).parent / "frontend"
    if not frontend_dir.is_dir():
        return

    static_url = f"/{DOMAIN}_static"
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(static_url, str(frontend_dir), cache_headers=True)]
        )
    except (RuntimeError, ValueError, AttributeError):
        pass

    versions = await hass.async_add_executor_job(_bundled_module_versions, frontend_dir)
    if not versions:
        return

    urls = {name: f"{static_url}/{name}?v={v}" for name, v in versions.items()}
    try:
        registered = await _async_register_lovelace_resources(hass, static_url, urls)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not register cards as Lovelace resources: %s", err)
        registered = False

    if not registered:
        # YAML-mode resources (or Lovelace unavailable): load via extra JS URLs.
        for name, url in urls.items():
            try:
                add_extra_js_url(hass, url)
            except Exception as fallback_err:  # noqa: BLE001
                _LOGGER.debug("Could not add %s extra URL: %s", name, fallback_err)


def _bundled_module_versions(frontend_dir: Path) -> dict[str, str]:
    """Return a cache-busting version for each bundled card present on disk.

    Vendored libraries (plotly, mushroom) get a per-file version. Every
    ``rivian-*`` module shares ONE combined version (a hash of all their
    mtimes and sizes plus the integration version): the cards import the
    shared ``rivian-vehicle-bar.js`` dynamically with their own ``?v=`` query
    string, which only works if every module carries the same value, and any
    change to one of them refreshes them all. Executor-bound (stats files).
    """
    versions: dict[str, str] = {}
    own: dict[str, tuple[int, int]] = {}
    for name in _BUNDLED_MODULES:
        try:
            stat = (frontend_dir / name).stat()
        except OSError:
            continue
        if name.startswith("rivian-"):
            own[name] = (int(stat.st_mtime), stat.st_size)
        else:
            versions[name] = f"{VERSION}-{int(stat.st_mtime):x}{stat.st_size:x}"
    if own:
        digest = hashlib.sha1(repr(sorted(own.items())).encode()).hexdigest()[:10]
        combined = f"{VERSION}-{digest}"
        versions.update({name: combined for name in own})
    return versions


async def _async_register_lovelace_resources(
    hass: HomeAssistant, static_url: str, urls: dict[str, str]
) -> bool:
    """Create or update storage-mode Lovelace resources for the bundled cards.

    Goes through Lovelace's live resource collection when it exists, so the
    change reaches the frontend without a restart; writing the storage file
    directly is invisible once that collection has been loaded. Returns False
    in YAML resource mode, where storage-mode resources are ignored.
    """
    lovelace = hass.data.get("lovelace")
    if getattr(lovelace, "resource_mode", "storage") == "yaml":
        return False

    resources = getattr(lovelace, "resources", None)
    if resources is not None and hasattr(resources, "async_create_item"):
        if not getattr(resources, "loaded", True):
            await resources.async_load()
            resources.loaded = True
        for name, url in urls.items():
            existing = next(
                (
                    item
                    for item in resources.async_items()
                    if f"{static_url}/{name}" in item.get("url", "")
                ),
                None,
            )
            if existing is None:
                await resources.async_create_item({"res_type": "module", "url": url})
            elif existing.get("url") != url:
                await resources.async_update_item(
                    existing["id"], {"res_type": "module", "url": url}
                )
        return True

    # Lovelace not set up yet: its collection will read this file when it loads.
    import uuid

    store = Store(hass, 1, "lovelace_resources")
    data = await store.async_load() or {"items": []}
    items = data.get("items", [])
    changed = False
    for name, url in urls.items():
        existing = next(
            (x for x in items if f"{static_url}/{name}" in x.get("url", "")), None
        )
        if existing is None:
            items.append({"id": uuid.uuid4().hex, "url": url, "type": "module"})
            changed = True
        elif existing.get("url") != url:
            existing["url"] = url
            changed = True
    if changed:
        data["items"] = items
        await store.async_save(data)
    return True


def _async_register_zone_listener(hass: HomeAssistant) -> None:
    """Resync every store's places when an HA zone is added/changed/removed.

    Registered once per Home Assistant instance (not per config entry),
    guarded the same way as the WebSocket API's registration flag. Listens to
    every ``state_changed`` event rather than a fixed entity list, since
    zones can be created or deleted at any time; a burst of changes (editing
    several zones, or a batch import) collapses into one resync via
    ``async_call_later`` debouncing.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_ZONE_LISTENER_REGISTERED_KEY):
        return
    domain_data[_ZONE_LISTENER_REGISTERED_KEY] = True

    cancel_debounce: list[Any] = [None]

    async def _resync_all_zones(_now: Any = None) -> None:
        zones = read_zone_states(hass)
        # Places are shared by every vehicle, so the zones sync once for the
        # real dataset (never the demo one), not once per store.
        stores = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
        ]
        if not stores:
            return
        try:
            await stores[0].async_sync_zones(zones)
        except Exception as err:  # noqa: BLE001 - a resync failure must not crash HA
            _LOGGER.warning("Zone resync failed: %s", err)
            return
        for store in stores:
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": store.vin})

    @callback
    def _on_state_changed(event: Event) -> None:
        entity_id = event.data.get("entity_id", "")
        if not entity_id.startswith("zone."):
            return
        if cancel_debounce[0] is not None:
            cancel_debounce[0]()
        cancel_debounce[0] = async_call_later(
            hass, ZONE_SYNC_DEBOUNCE_SECONDS, _resync_all_zones
        )

    domain_data[_ZONE_LISTENER_REMOVE_KEY] = hass.bus.async_listen(
        "state_changed", _on_state_changed
    )


def _make_drive_complete_listener(
    hass: HomeAssistant, vin: str, store: DriveStore
) -> Any:
    """Build a DriveTracker listener that reacts to a finished drive.

    On ``DriveEvent.DRIVE_COMPLETE`` it fires the ``rivian_analytics_updated``
    bus event (so the frontend series card refetches) and schedules a
    long-term statistics update as a background task. The listener itself is
    a synchronous ``@callback`` -- it never awaits -- so it cannot block the
    DriveTracker's notification loop; the actual (async, executor-bound)
    statistics write happens later on the event loop via the scheduled task.
    """

    @callback
    def _on_drive_event(_drive_state: Any, event: DriveEvent) -> None:
        if event != DriveEvent.DRIVE_COMPLETE:
            return

        hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": vin})

        last_drive = store.last_drive
        if last_drive is not None:
            hass.async_create_task(
                async_update_statistics(hass, vin, [last_drive]),
                f"rivian_statistics_update_{vin}",
            )

    return _on_drive_event


async def _async_prune_analytics_retention(
    hass: HomeAssistant, entry: ConfigEntry, _now: Any = None
) -> None:
    """Prune analytics history older than the configured retention windows.

    Drive/vampire-drain retention (``analytics_retention_days``) and GPS
    track retention/thinning (``track_retention_days``/
    ``track_full_detail_days``) are independent settings -- a retention of
    ``0`` days means "keep forever" for whichever one it applies to, so each
    prune step runs (or is skipped) on its own rather than one governing
    both. Runs on the event loop but every blocking step (the SQLite
    delete/thin and the cache rebuild) is delegated to the executor by
    ``DriveStore.async_prune``/``async_prune_tracks``.

    Also refits each VIN's anchored energy-model coefficients once per call
    (this function runs once shortly after setup and then every 24 hours),
    via ``DriveStore.async_fit_energy_model``.
    """
    retention_days = entry.options.get(
        CONF_ANALYTICS_RETENTION_DAYS, DEFAULT_ANALYTICS_RETENTION_DAYS
    )
    track_retention_days = entry.options.get(
        CONF_TRACK_RETENTION_DAYS, DEFAULT_TRACK_RETENTION_DAYS
    )
    track_full_detail_days = entry.options.get(
        CONF_TRACK_FULL_DETAIL_DAYS, DEFAULT_TRACK_FULL_DETAIL_DAYS
    )

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(entry_data, dict):
        return

    for store in entry_data.get(ATTR_DRIVE_STORE, {}).values():
        if retention_days:
            try:
                removed = await store.async_prune(retention_days)
                if removed:
                    _LOGGER.debug(
                        "Pruned %d analytics rows for VIN %s (retention=%d days)",
                        removed,
                        store.vin,
                        retention_days,
                    )
            except Exception as err:  # noqa: BLE001 - a prune failure must not crash HA
                _LOGGER.warning(
                    "Analytics retention prune failed for VIN %s: %s", store.vin, err
                )

        try:
            track_counts = await store.async_prune_tracks(
                track_retention_days, track_full_detail_days
            )
            if track_counts and (
                track_counts.get("deleted") or track_counts.get("thinned")
            ):
                _LOGGER.debug(
                    "Pruned/thinned GPS tracks for VIN %s: %s", store.vin, track_counts
                )
        except Exception as err:  # noqa: BLE001 - a prune failure must not crash HA
            _LOGGER.warning(
                "Track retention prune failed for VIN %s: %s", store.vin, err
            )

        try:
            result = await store.async_fit_energy_model()
            _LOGGER.debug("Daily energy-model refit for VIN %s: %s", store.vin, result)
        except Exception as err:  # noqa: BLE001 - a fit failure must not crash HA
            _LOGGER.warning("Energy-model refit failed for VIN %s: %s", store.vin, err)

    # The Rivian-app session list (at most once a day), station lookups for
    # fast charges, and the battery-capacity history (never pruned).
    try:
        await _async_charging_history_job(hass, entry_data)
    except Exception as err:  # noqa: BLE001 - must never crash HA
        _LOGGER.warning("Charging history job failed: %s", err)


async def _async_charging_history_job(
    hass: HomeAssistant, entry_data: dict[str, Any], force: bool = False
) -> dict[str, Any]:
    """Import the Rivian app's charging history and refresh the capacity history.

    Real vehicles only: demo vehicles are never imported. ``force`` skips the
    once-a-day guard (the ``rivian.import_charging_history`` service).
    """
    stores: dict[str, DriveStore] = entry_data.get(ATTR_DRIVE_STORE) or {}
    vehicles: dict[str, Any] = entry_data.get(ATTR_VEHICLE) or {}
    client = entry_data.get(ATTR_API)
    vins_by_id = {
        vehicle_id: str(info["vin"])
        for vehicle_id, info in vehicles.items()
        if info.get("vin")
        and vehicle_id in stores
        and not stores[vehicle_id].is_demo
        and not is_demo_vin(hass, str(info["vin"]))
    }
    if not vins_by_id:
        return {"vehicles": {}}
    db = next(iter(stores.values()))._db
    # The OpenStreetMap station lookup follows the "place naming" option,
    # like every other lookup that sends a location out.
    lookup_stations = any(
        stores[vehicle_id]._place_geocoding for vehicle_id in vins_by_id
    )
    result: dict[str, Any] = {}
    if client is not None:
        result = await charging_history.async_run_history_job(
            hass, client, vins_by_id, db, force, lookup_stations
        )
    capacity: dict[str, int] = {}
    for vin in vins_by_id.values():
        capacity[vin] = await charging_history.async_update_capacity_history(
            hass, db, vin
        )
    result["capacity_rows"] = capacity
    # Outside temperature where each located session charged (Open-Meteo).
    weather: dict[str, int] = {}
    for vehicle_id, vin in vins_by_id.items():
        try:
            filled = await stores[vehicle_id].async_fill_session_temperatures()
        except Exception:
            _LOGGER.exception("Charging weather lookup failed for VIN %s", vin)
            continue
        weather[vin] = int(filled.get("updated") or 0)
    result["session_weather"] = weather
    # Charges the battery-level history shows that nothing recorded (they
    # fire the update event themselves when the stored set changed).
    inferred: dict[str, Any] = {}
    for vehicle_id, vin in vins_by_id.items():
        try:
            inferred[vin] = await stores[vehicle_id].async_infer_charging_sessions()
        except Exception:
            _LOGGER.exception("Inferring charging sessions failed for VIN %s", vin)
    result["inferred"] = inferred
    changed = any(
        (result.get("vehicles") or {}).get(vin, {}).get("matched")
        or (result.get("vehicles") or {}).get(vin, {}).get("inserted")
        or (result.get("stations") or {}).get(vin)
        or capacity.get(vin)
        or weather.get(vin)
        for vin in vins_by_id.values()
    )
    if changed:
        for vin in vins_by_id.values():
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": vin})
    return result


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entries."""
    _LOGGER.info(
        "Rivian integration is starting under version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )

    hass.data.setdefault(DOMAIN, {})

    analytics_db = await _async_get_or_create_analytics_db(hass)
    _async_check_analytics_db_issues(hass, analytics_db)
    await async_setup_demo_registry(hass, analytics_db)

    client = get_rivian_api_from_entry(hass, entry)
    try:
        await client.create_csrf_token()
    except Exception as err:
        _LOGGER.error("Could not update Rivian Data: %s", err, exc_info=1)
        await client.close()
        raise ConfigEntryNotReady("Error communicating with API") from err

    coordinator = UserCoordinator(
        hass=hass, config_entry=entry, client=client, include_phones=True
    )
    await coordinator.async_config_entry_first_refresh()

    vehicle_control = entry.options.get(CONF_VEHICLE_CONTROL)
    if vehicle_control and not coordinator.data.get("registrationChannels"):
        vehicle_control = []
        async_create_issue(
            hass,
            DOMAIN,
            entry.entry_id,
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key="2fa_missing",
        )
    else:
        async_delete_issue(hass, DOMAIN, entry.entry_id)

    vehicles = coordinator.get_vehicles()
    if vehicle_control and (
        enrolled := coordinator.get_enrolled_phone_data(entry.options.get("public_key"))
    ):
        for vehicle_id in vehicles:
            if vehicle_id in enrolled[1]:
                vehicles[vehicle_id]["phone_identity_id"] = enrolled[1][vehicle_id]

    vehicle_coordinators: dict[str, VehicleCoordinator] = {}
    drive_stores: dict[str, DriveStore] = {}
    drive_trackers: dict[str, DriveTracker] = {}
    for vehicle_id in vehicles:
        coor = VehicleCoordinator(
            hass=hass, config_entry=entry, client=client, vehicle_id=vehicle_id
        )
        await coor.async_config_entry_first_refresh()
        if not coor.data:
            raise ConfigEntryNotReady("Issue loading vehicle data")
        await coor.charging_coordinator.async_config_entry_first_refresh()
        await coor.drivers_coordinator.async_config_entry_first_refresh()
        vehicle_coordinators[vehicle_id] = coor

        vehicle_info = vehicles[vehicle_id]
        vin = str(vehicle_info.get("vin", vehicle_id))
        place_geocoding = entry.options.get(
            CONF_PLACE_GEOCODING, DEFAULT_PLACE_GEOCODING
        )
        store = DriveStore(
            hass=hass, vin=vin, db=analytics_db, place_geocoding=place_geocoding
        )
        tracker = DriveTracker(
            hass=hass,
            entry=entry,
            coordinator=coor,
            vehicle_info=vehicle_info,
            store=store,
        )
        # Listen before setup: setup may finalize a drive recovered from a
        # mid-drive checkpoint, and that drive needs statistics too.
        entry.async_on_unload(
            tracker.async_add_listener(_make_drive_complete_listener(hass, vin, store))
        )
        await tracker.async_setup()
        drive_stores[vehicle_id] = store
        drive_trackers[vehicle_id] = tracker

    wallbox_coordinator = WallboxCoordinator(
        hass=hass, config_entry=entry, client=client
    )
    await wallbox_coordinator.async_config_entry_first_refresh()

    _async_register_zone_listener(hass)
    zones = read_zone_states(hass)
    first_store = next(iter(drive_stores.values()), None)
    if first_store is not None:
        # One sync for the whole (real) dataset: places belong to no vehicle.
        try:
            await first_store.async_sync_zones(zones)
        except Exception as err:  # noqa: BLE001 - a sync failure must not block setup
            _LOGGER.warning("Initial zone sync failed: %s", err)

    hass.data[DOMAIN][entry.entry_id] = {
        ATTR_API: client,
        ATTR_VEHICLE: vehicles,
        ATTR_COORDINATOR: {
            ATTR_USER: coordinator,
            ATTR_VEHICLE: vehicle_coordinators,
            ATTR_WALLBOX: wallbox_coordinator,
        },
        ATTR_DRIVE_TRACKER: drive_trackers,
        ATTR_DRIVE_STORE: drive_stores,
    }

    async def async_handle_backfill(call: ServiceCall) -> None:
        """Handle backfill historical drives service call."""
        vin = call.data.get("vin")
        days = call.data.get("days")
        dry_run = call.data.get("dry_run", True)
        db_path = call.data.get("db_path")
        tracks = call.data.get("tracks", True)

        target_vins: list[str] = []
        if vin:
            target_vins.append(vin)
        else:
            for entry_data in _iter_entry_datas(hass):
                if ATTR_VEHICLE in entry_data:
                    for v_info in entry_data[ATTR_VEHICLE].values():
                        v_vin = str(v_info.get("vin", ""))
                        if v_vin and v_vin not in target_vins:
                            target_vins.append(v_vin)

        if not target_vins:
            _LOGGER.warning("No Rivian vehicles configured to backfill")
            return

        for target_vin in target_vins:
            _LOGGER.info(
                "Starting historical drive backfill for VIN %s (dry_run=%s)",
                target_vin,
                dry_run,
            )
            matched_store = None
            matched_tracker = None
            for entry_data in _iter_entry_datas(hass):
                trackers = entry_data.get(ATTR_DRIVE_TRACKER, {})
                stores = entry_data.get(ATTR_DRIVE_STORE, {})
                for v_id, trk in trackers.items():
                    if trk.vin == target_vin:
                        matched_tracker = trk
                        matched_store = stores.get(v_id) or trk.store
                        break

            await async_backfill_from_recorder(
                hass=hass,
                vin=target_vin,
                days=days,
                dry_run=dry_run,
                db_path=db_path,
                store=matched_store,
                tracks=tracks,
            )

            if not dry_run and matched_store is not None:
                try:
                    await matched_store.async_rebuild_places()
                    await matched_store.async_rebuild_routes()
                    hass.bus.async_fire(
                        RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": target_vin}
                    )
                except Exception as err:  # noqa: BLE001 - must not fail the service call
                    _LOGGER.warning(
                        "Places/routes rebuild after backfill failed for VIN %s: %s",
                        target_vin,
                        err,
                    )

            if not dry_run and matched_tracker is not None:
                await matched_tracker.store.async_load()
                matched_tracker._notify_listeners()

    async def async_handle_set_vehicle_picture(call: ServiceCall) -> None:
        """Download a picture from a URL once and keep it as the vehicle's picture."""
        vin = call.data.get("vin")
        targets = [
            (entry_id, store)
            for entry_id, entry_data in hass.data.get(DOMAIN, {}).items()
            if entry_id not in _SPECIAL_DOMAIN_DATA_KEYS
            and isinstance(entry_data, dict)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if vin is None or store.vin == vin
        ]
        if not targets:
            raise HomeAssistantError(f"No Rivian vehicle with VIN {vin}")
        if len(targets) > 1:
            raise HomeAssistantError(
                "Several Rivian vehicles are set up; say which one with `vin`"
            )
        entry_id, store = targets[0]
        try:
            picture = await async_picture_from_url(hass, call.data["url"])
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await store.async_save_vehicle_picture(picture)
        async_apply_vehicle_picture(hass, entry_id, store.vin, picture)

    async def async_handle_create_dashboard(call: ServiceCall) -> None:
        """Handle the service call to create or update the turnkey efficiency dashboard."""
        title = call.data.get("title", DEFAULT_TITLE)
        icon = call.data.get("icon", DEFAULT_ICON)
        url_path = call.data.get("url_path", DEFAULT_URL_PATH)
        await async_create_efficiency_dashboard(
            hass=hass,
            title=title,
            icon=icon,
            url_path=url_path,
        )
        # The dashboard was just (re)generated on the latest schema: clear the
        # staleness repair immediately instead of leaving it until next restart.
        try:
            await _async_check_dashboard_staleness(hass)
        except Exception as err:  # noqa: BLE001 - must never fail the service call
            _LOGGER.debug("Dashboard staleness check raised unexpectedly: %s", err)

    async def async_handle_rebuild_heat_map(call: ServiceCall) -> None:
        """Handle the service call to recount the road heat map from stored routes."""
        vin = call.data.get("vin")
        targets = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if vin is None or store.vin == vin
        ]
        if not targets:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}"
                if vin
                else "No Rivian vehicles with drive history"
            )
        for store in targets:
            result = await store.async_rebuild_heat()
            _LOGGER.info("Rebuilt road heat map for VIN %s: %s", store.vin, result)
            # Open Drives cards redraw their heat map on this event.
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": store.vin})

    async def async_handle_recompute_drive_stats(call: ServiceCall) -> None:
        """Handle the service call to recompute per-drive summary stats from stored tracks.

        Only the track-derived columns (moving/stopped time, stop count,
        climb/descent, robust max speed, highway share) are touched;
        live-only vehicle context (range, drive modes, trailer, driver) is
        never recomputed here. Thinned tracks (older routes simplified to
        ~10 m) give slightly less accurate stop/climb numbers than a
        full-detail route.
        """
        vin = call.data.get("vin")
        targets = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if vin is None or store.vin == vin
        ]
        if not targets:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}"
                if vin
                else "No Rivian vehicles with drive history"
            )
        for store in targets:
            result = await store.async_recompute_stats()
            _LOGGER.info(
                "Recomputed drive summary stats for VIN %s: %s", store.vin, result
            )
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": store.vin})

    async def async_handle_fit_energy_model(call: ServiceCall) -> None:
        """Handle the service call to refit the anchored energy model from recent drives.

        Recent routed drives supply the shape (aero/rolling/grade/kinetic
        physics); each drive's measured SoC-drop energy re-anchors it. If a
        VIN has too few qualifying drives in the fitting window, its existing
        fit (if any) is left untouched.
        """
        vin = call.data.get("vin")
        targets = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if vin is None or store.vin == vin
        ]
        if not targets:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}"
                if vin
                else "No Rivian vehicles with drive history"
            )
        for store in targets:
            result = await store.async_fit_energy_model()
            _LOGGER.info("Fitted energy model for VIN %s: %s", store.vin, result)
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": store.vin})

    async def async_handle_backfill_weather(call: ServiceCall) -> None:
        """Handle the service call to fill wind/precipitation/pressure/humidity/air density.

        Add-only: fills only the still-empty condition columns (and the
        energy model's expected kWh) of stored routed drives from the
        Open-Meteo archive, rate-limited. Demo vehicles are skipped.
        """
        vin = call.data.get("vin")
        days = call.data.get("days", 365)
        targets = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if (vin is None or store.vin == vin) and not store.is_demo
        ]
        if not targets:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}"
                if vin
                else "No Rivian vehicles with drive history"
            )
        for store in targets:
            result = await store.async_backfill_weather(days)
            _LOGGER.info("Weather backfill for VIN %s: %s", store.vin, result)
            if result.get("complete"):
                await hass.async_add_executor_job(
                    store._db.mark_weather_seeded, store.vin
                )
            if result.get("updated"):
                hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": store.vin})

    async def async_handle_snap_route_gaps(call: ServiceCall) -> None:
        """Handle the service call to snap recorded GPS gaps onto OSM roads."""
        vin = call.data.get("vin")
        targets = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if vin is None or store.vin == vin
        ]
        if not targets:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}"
                if vin
                else "No Rivian vehicles with drive history"
            )
        for store in targets:
            result = await store.async_snap_gaps()
            _LOGGER.info("Snapped route gaps for VIN %s: %s", store.vin, result)
            if result.get("heat_stale_drives"):
                await store.async_rebuild_heat()
            if result.get("added") or result.get("heat_stale_drives"):
                hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": store.vin})

    async def async_handle_rebuild_places(call: ServiceCall) -> None:
        """Handle the service call to rebuild favorite places from stored drives."""
        vin = call.data.get("vin")
        targets = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if vin is None or store.vin == vin
        ]
        if not targets:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}"
                if vin
                else "No Rivian vehicles with drive history"
            )
        # Places are shared by every vehicle: rebuild once, refresh them all.
        result = await targets[0].async_rebuild_places()
        _LOGGER.info("Rebuilt favorite places: %s", result)
        for store in targets:
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": store.vin})

    async def async_handle_import_charging_history(call: ServiceCall) -> None:
        """Handle the service call to import the Rivian app's completed sessions.

        Matches each completed session to the stored ones (enriching vendor,
        home/public and energy) or inserts the missing ones, then looks up
        station details for fast charges from OpenStreetMap. Real vehicles only.
        """
        vin = call.data.get("vin")
        entries = [
            entry_data
            for entry_data in _iter_entry_datas(hass)
            if ATTR_DRIVE_STORE in entry_data
            and (
                vin is None
                or any(
                    str(v.get("vin")) == vin
                    for v in (entry_data.get(ATTR_VEHICLE) or {}).values()
                )
            )
        ]
        if not entries:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}" if vin else "No Rivian vehicles"
            )
        for entry_data in entries:
            result = await _async_charging_history_job(hass, entry_data, force=True)
            _LOGGER.info("Charging history import: %s", result)

    async def async_handle_infer_charging_sessions(call: ServiceCall) -> None:
        """Handle the service call to infer charging sessions from battery history."""
        vin = call.data.get("vin")
        targets = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if (vin is None or store.vin == vin) and not store.is_demo
        ]
        if not targets:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}" if vin else "No Rivian vehicles"
            )
        for store in targets:
            # On demand = a whole-history re-scan (the nightly run only
            # re-checks the last few days).
            result = await store.async_infer_charging_sessions(full=True)
            _LOGGER.info("Inferred charging sessions for %s: %s", store.vin, result)

    async def async_handle_rebuild_routes(call: ServiceCall) -> None:
        """Handle the service call to rebuild favorite drives (repeated routes)."""
        vin = call.data.get("vin")
        targets = [
            store
            for entry_data in _iter_entry_datas(hass)
            for store in (entry_data.get(ATTR_DRIVE_STORE) or {}).values()
            if vin is None or store.vin == vin
        ]
        if not targets:
            raise HomeAssistantError(
                f"No Rivian vehicle with VIN {vin}"
                if vin
                else "No Rivian vehicles with drive history"
            )
        # Routes are shared by every vehicle: rebuild once, refresh them all.
        result = await targets[0].async_rebuild_routes()
        _LOGGER.info("Rebuilt favorite drives: %s", result)
        for store in targets:
            hass.bus.async_fire(RIVIAN_ANALYTICS_UPDATED_EVENT, {"vin": store.vin})

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL_DRIVE_HISTORY,
            async_handle_backfill,
            schema=BACKFILL_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_VEHICLE_PICTURE):
        # Admin only: it makes Home Assistant fetch an arbitrary URL.
        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_SET_VEHICLE_PICTURE,
            async_handle_set_vehicle_picture,
            schema=SET_VEHICLE_PICTURE_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CREATE_EFFICIENCY_DASHBOARD):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CREATE_EFFICIENCY_DASHBOARD,
            async_handle_create_dashboard,
            schema=CREATE_DASHBOARD_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REBUILD_HEAT_MAP):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REBUILD_HEAT_MAP,
            async_handle_rebuild_heat_map,
            schema=REBUILD_HEAT_MAP_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RECOMPUTE_DRIVE_STATS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RECOMPUTE_DRIVE_STATS,
            async_handle_recompute_drive_stats,
            schema=RECOMPUTE_DRIVE_STATS_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_FIT_ENERGY_MODEL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FIT_ENERGY_MODEL,
            async_handle_fit_energy_model,
            schema=FIT_ENERGY_MODEL_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL_WEATHER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL_WEATHER,
            async_handle_backfill_weather,
            schema=BACKFILL_WEATHER_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SNAP_ROUTE_GAPS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SNAP_ROUTE_GAPS,
            async_handle_snap_route_gaps,
            schema=SNAP_ROUTE_GAPS_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REBUILD_PLACES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REBUILD_PLACES,
            async_handle_rebuild_places,
            schema=REBUILD_PLACES_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_CHARGING_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_CHARGING_HISTORY,
            async_handle_import_charging_history,
            schema=IMPORT_CHARGING_HISTORY_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_INFER_CHARGING_SESSIONS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_INFER_CHARGING_SESSIONS,
            async_handle_infer_charging_sessions,
            schema=INFER_CHARGING_SESSIONS_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REBUILD_ROUTES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REBUILD_ROUTES,
            async_handle_rebuild_routes,
            schema=REBUILD_ROUTES_SERVICE_SCHEMA,
        )

    async def async_handle_create_demo_data(call: ServiceCall) -> None:
        """Install (or replace) the synthetic Eagle, ID demo vehicles. Admin only."""
        installed = await async_install_demo(hass)
        _LOGGER.info("Installed demo vehicles: %s", installed)

    async def async_handle_delete_demo_data(call: ServiceCall) -> None:
        """Remove the demo vehicles (or one by VIN) completely. Admin only."""
        removed = await async_remove_demo(hass, call.data.get("vin"))
        _LOGGER.info("Removed demo vehicles: %s", removed)

    if not hass.services.has_service(DOMAIN, SERVICE_CREATE_DEMO_DATA):
        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_CREATE_DEMO_DATA,
            async_handle_create_demo_data,
            schema=CREATE_DEMO_DATA_SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_DELETE_DEMO_DATA):
        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_DELETE_DEMO_DATA,
            async_handle_delete_demo_data,
            schema=DELETE_DEMO_DATA_SERVICE_SCHEMA,
        )

    async_register_websocket_api(hass)
    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _schedule_dashboard_autocreate(hass)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    async def _run_retention_prune(now: Any = None) -> None:
        await _async_prune_analytics_retention(hass, entry, now)

    entry.async_on_unload(
        async_call_later(
            hass, RETENTION_PRUNE_INITIAL_DELAY_SECONDS, _run_retention_prune
        )
    )
    entry.async_on_unload(
        async_track_time_interval(hass, _run_retention_prune, RETENTION_PRUNE_INTERVAL)
    )

    try:
        await _async_check_dashboard_staleness(hass)
    except Exception as err:  # noqa: BLE001 - must never fail setup
        _LOGGER.debug("Dashboard staleness check raised unexpectedly: %s", err)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    if drive_trackers := entry_data.get(ATTR_DRIVE_TRACKER):
        for tracker in drive_trackers.values():
            await tracker.async_unload()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    api: Rivian | None = entry_data.get(ATTR_API)
    if api:
        await api.close()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    if not _iter_entry_datas(hass):
        # No config entries left: this is the last one out, so tear down the
        # shared, instance-wide state (services, websocket API flag, and the
        # AnalyticsDatabase connection itself).
        if hass.services.has_service(DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY):
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_DRIVE_HISTORY)
        if hass.services.has_service(DOMAIN, SERVICE_CREATE_EFFICIENCY_DASHBOARD):
            hass.services.async_remove(DOMAIN, SERVICE_CREATE_EFFICIENCY_DASHBOARD)
        if hass.services.has_service(DOMAIN, SERVICE_SET_VEHICLE_PICTURE):
            hass.services.async_remove(DOMAIN, SERVICE_SET_VEHICLE_PICTURE)
        if hass.services.has_service(DOMAIN, SERVICE_REBUILD_HEAT_MAP):
            hass.services.async_remove(DOMAIN, SERVICE_REBUILD_HEAT_MAP)
        if hass.services.has_service(DOMAIN, SERVICE_RECOMPUTE_DRIVE_STATS):
            hass.services.async_remove(DOMAIN, SERVICE_RECOMPUTE_DRIVE_STATS)
        if hass.services.has_service(DOMAIN, SERVICE_FIT_ENERGY_MODEL):
            hass.services.async_remove(DOMAIN, SERVICE_FIT_ENERGY_MODEL)
        if hass.services.has_service(DOMAIN, SERVICE_BACKFILL_WEATHER):
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_WEATHER)
        if hass.services.has_service(DOMAIN, SERVICE_SNAP_ROUTE_GAPS):
            hass.services.async_remove(DOMAIN, SERVICE_SNAP_ROUTE_GAPS)
        if hass.services.has_service(DOMAIN, SERVICE_REBUILD_PLACES):
            hass.services.async_remove(DOMAIN, SERVICE_REBUILD_PLACES)
        if hass.services.has_service(DOMAIN, SERVICE_REBUILD_ROUTES):
            hass.services.async_remove(DOMAIN, SERVICE_REBUILD_ROUTES)
        if hass.services.has_service(DOMAIN, SERVICE_IMPORT_CHARGING_HISTORY):
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_CHARGING_HISTORY)
        if hass.services.has_service(DOMAIN, SERVICE_INFER_CHARGING_SESSIONS):
            hass.services.async_remove(DOMAIN, SERVICE_INFER_CHARGING_SESSIONS)
        for demo_service in (SERVICE_CREATE_DEMO_DATA, SERVICE_DELETE_DEMO_DATA):
            if hass.services.has_service(DOMAIN, demo_service):
                hass.services.async_remove(DOMAIN, demo_service)
        async_teardown_demo_registry(hass)

        domain_data = hass.data.get(DOMAIN, {})
        db: AnalyticsDatabase | None = domain_data.pop(ATTR_ANALYTICS_DB, None)
        domain_data.pop(_ANALYTICS_DB_LOCK_KEY, None)
        domain_data.pop(_DASHBOARD_AUTOCREATE_KEY, None)
        domain_data.pop("_ws_api_registered", None)
        remove_zone_listener = domain_data.pop(_ZONE_LISTENER_REMOVE_KEY, None)
        if remove_zone_listener is not None:
            remove_zone_listener()
        domain_data.pop(_ZONE_LISTENER_REGISTERED_KEY, None)
        if db is not None:
            await hass.async_add_executor_job(db.close)
        if not domain_data:
            hass.data.pop(DOMAIN, None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    if public_key := entry.options.get("public_key"):
        client = get_rivian_api_from_entry(hass, entry)
        coordinator = UserCoordinator(
            hass=hass, config_entry=entry, client=client, include_phones=True
        )
        await coordinator.async_config_entry_first_refresh()

        if enrolled_data := coordinator.get_enrolled_phone_data(public_key=public_key):
            for identity_id in enrolled_data[1].values():
                await client.disenroll_phone(identity_id=identity_id)
        await client.close()


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    coordinators = hass.data[DOMAIN][config_entry.entry_id][ATTR_COORDINATOR]
    user_coordinator: UserCoordinator = coordinators[ATTR_USER]
    wallbox_coordinator: WallboxCoordinator = coordinators[ATTR_WALLBOX]

    vehicles = user_coordinator.get_vehicles().keys()
    wallboxes = {x["wallboxId"] for x in wallbox_coordinator.data}

    return not any(
        identifier
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN and identifier[1] in vehicles | wallboxes
    )
