"""Tests for the tabbed Rivian dashboard generator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian.const import (
    ATTR_VEHICLE,
    DASHBOARD_SCHEMA_VERSION,
    DOMAIN,
)
from custom_components.rivian.dashboard_generator import (
    ENTITY_KEY_MAP,
    _async_resolve_vehicle_entities,
    async_create_efficiency_dashboard,
    async_discover_vehicle_prefixes,
)

TEST_VIN = "7PDSGABA8NN000000"
OTHER_VIN = "7PDSGABA8NN111111"


def _strings(node: Any) -> list[str]:
    """Every string anywhere in a card config tree."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


class _MockEntityRegistryEntry:
    """Minimal stand-in for a homeassistant entity registry RegistryEntry."""

    def __init__(self, unique_id: str) -> None:
        self.unique_id = unique_id


class _MockEntityRegistry:
    """Minimal stand-in for homeassistant.helpers.entity_registry.EntityRegistry."""

    def __init__(
        self,
        entries: dict[str, str] | None = None,
        entity_ids: dict[tuple[str, str, str], str] | None = None,
    ) -> None:
        self._entries = entries or {}
        self._entity_ids = entity_ids or {}

    def async_get(self, entity_id: str) -> _MockEntityRegistryEntry | None:
        unique_id = self._entries.get(entity_id)
        if unique_id is None:
            return None
        return _MockEntityRegistryEntry(unique_id)

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        return self._entity_ids.get((domain, platform, unique_id))


def _core_entity_ids(
    vin: str, slug: str, keys: list[str] | None = None
) -> dict[tuple[str, str, str], str]:
    """Build entity_ids for a subset (default: all) of ENTITY_KEY_MAP for one VIN."""
    wanted = keys if keys is not None else list(ENTITY_KEY_MAP)
    return {
        (domain, DOMAIN, f"{vin}-{key}"): f"{domain}.{slug}_{name.replace('-', '_')}"
        for name in wanted
        for domain, key in [ENTITY_KEY_MAP[name]]
    }


def _er_patch(registry: _MockEntityRegistry) -> Any:
    """Patch target for `custom_components.rivian.dashboard_generator.er`."""
    return patch(
        "custom_components.rivian.dashboard_generator.er",
        MagicMock(async_get=MagicMock(return_value=registry)),
    )


@pytest.mark.asyncio
async def test_resolve_vehicle_entities_returns_only_resolved_keys() -> None:
    """Unresolved (missing or non-string) registry lookups are skipped, never guessed."""
    hass = MagicMock()
    registry = _MockEntityRegistry(
        entity_ids=_core_entity_ids(TEST_VIN, "rivi", ["soc", "range", "location"])
    )
    with _er_patch(registry):
        resolved = await _async_resolve_vehicle_entities(hass, TEST_VIN)

    assert resolved == {
        "soc": "sensor.rivi_soc",
        "range": "sensor.rivi_range",
        "location": "device_tracker.rivi_location",
    }
    # Every other logical key was never registered, so it's absent, not "".
    assert "soc_limit" not in resolved
    assert "" not in resolved.values()


@pytest.mark.asyncio
async def test_resolve_vehicle_entities_ignores_magicmock_results() -> None:
    """A MagicMock (unconfigured) registry response is treated as unresolved."""
    hass = MagicMock()
    registry = MagicMock()
    registry.async_get_entity_id.return_value = MagicMock()  # not a str
    with patch(
        "custom_components.rivian.dashboard_generator.er",
        MagicMock(async_get=MagicMock(return_value=registry)),
    ):
        resolved = await _async_resolve_vehicle_entities(hass, TEST_VIN)

    assert resolved == {}


@pytest.mark.asyncio
async def test_resolve_vehicle_entities_empty_vin_short_circuits() -> None:
    hass = MagicMock()
    assert await _async_resolve_vehicle_entities(hass, "") == {}


@pytest.mark.asyncio
async def test_async_discover_vehicle_prefixes() -> None:
    """Test vehicle prefix discovery from Home Assistant state machine."""
    hass = MagicMock()
    hass.data = {}
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_last_drive_efficiency",
        "sensor.rivian_r1s_battery_state_of_charge",
    ]

    registry = _MockEntityRegistry(
        {
            "sensor.rivian_r1s_last_drive_efficiency": (
                f"{TEST_VIN}-last_drive_efficiency"
            )
        }
    )

    with patch(
        "custom_components.rivian.dashboard_generator.er",
        MagicMock(async_get=MagicMock(return_value=registry)),
    ):
        prefixes = await async_discover_vehicle_prefixes(hass)

    assert len(prefixes) == 1
    name, prefix, vin, entry_id = prefixes[0]
    assert "Rivian R1S" in name
    assert prefix == "sensor.rivian_r1s_"
    assert vin == TEST_VIN
    assert entry_id == ""  # fallback discovery has no entry context


@pytest.mark.asyncio
async def test_discovery_maps_each_vehicle_to_its_own_entities() -> None:
    """Two vehicles whose entity ids both contain "rivian" must not be crossed."""
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "entry1": {
                ATTR_VEHICLE: {
                    "v1": {"name": "Rivi", "vin": TEST_VIN},
                    "v2": {"name": "Otto", "vin": OTHER_VIN},
                }
            }
        }
    }
    # Default entity ids that match neither nickname, only "rivian".
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_last_drive_efficiency",
        "sensor.rivian_r1t_last_drive_efficiency",
    ]
    registry = _MockEntityRegistry(
        entity_ids={
            ("sensor", DOMAIN, f"{TEST_VIN}-last_drive_efficiency"): (
                "sensor.rivian_r1s_last_drive_efficiency"
            ),
            ("sensor", DOMAIN, f"{OTHER_VIN}-last_drive_efficiency"): (
                "sensor.rivian_r1t_last_drive_efficiency"
            ),
        }
    )

    with patch(
        "custom_components.rivian.dashboard_generator.er",
        MagicMock(async_get=MagicMock(return_value=registry)),
    ):
        prefixes = await async_discover_vehicle_prefixes(hass)

    assert {(name, prefix, vin) for name, prefix, vin, _ in prefixes} == {
        ("Rivi", "sensor.rivian_r1s_", TEST_VIN),
        ("Otto", "sensor.rivian_r1t_", OTHER_VIN),
    }


def _grid_dashboards_store(saved: dict[str, Any]) -> type:
    """Build a MockStore class backed by the given dict, for patching Store."""

    class MockStore:
        def __init__(self, _hass: Any, _version: int, key: str) -> None:
            self.key = key

        async def async_load(self) -> Any:
            return saved.get(self.key, {"items": []})

        async def async_save(self, data: Any) -> None:
            saved[self.key] = data

    return MockStore


def _hass_with_one_vehicle(name: str = "Rivi", vin: str = TEST_VIN) -> MagicMock:
    """Build a MagicMock hass discoverable via the primary (hass.data) path."""
    hass = MagicMock()
    slug = name.lower().replace(" ", "_")
    hass.data = {
        DOMAIN: {"entry1": {ATTR_VEHICLE: {"vehicle1": {"name": name, "vin": vin}}}}
    }
    hass.states.async_entity_ids.return_value = [f"sensor.{slug}_last_drive_efficiency"]
    hass.config_entries.async_entries.return_value = []
    return hass


def _hass_with_two_vehicles(
    names: tuple[str, str] = ("Rivi", "Otto"),
    vins: tuple[str, str] = (TEST_VIN, OTHER_VIN),
) -> tuple[MagicMock, _MockEntityRegistry]:
    """Build a MagicMock hass with two vehicles under one config entry."""
    hass = MagicMock()
    name1, name2 = names
    vin1, vin2 = vins
    slug1, slug2 = name1.lower().replace(" ", "_"), name2.lower().replace(" ", "_")
    hass.data = {
        DOMAIN: {
            "entry1": {
                ATTR_VEHICLE: {
                    "vehicle1": {"name": name1, "vin": vin1},
                    "vehicle2": {"name": name2, "vin": vin2},
                }
            }
        }
    }
    hass.states.async_entity_ids.return_value = [
        f"sensor.{slug1}_last_drive_efficiency",
        f"sensor.{slug2}_last_drive_efficiency",
    ]
    hass.config_entries.async_entries.return_value = []

    return hass, _MockEntityRegistry()


def _find_cards(node: Any) -> list[dict[str, Any]]:
    """Recursively collect all card dicts from a nested Lovelace structure."""
    cards: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "type" in node:
            cards.append(node)
        for val in node.values():
            cards.extend(_find_cards(val))
    elif isinstance(node, list):
        for item in node:
            cards.extend(_find_cards(item))
    return cards


@pytest.mark.asyncio
async def test_one_vehicle_produces_four_tabs_in_order_with_no_picker() -> None:
    """A single vehicle gets a plain 4-tab dashboard: no picker, no conditionals."""
    hass = _hass_with_one_vehicle()
    saved: dict[str, Any] = {}
    registry = _MockEntityRegistry()

    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store(saved),
        ),
        patch(
            "custom_components.rivian.dashboard_generator.er",
            MagicMock(async_get=MagicMock(return_value=registry)),
        ),
    ):
        await async_create_efficiency_dashboard(hass=hass)

    config = saved["lovelace.rivian_dashboard"]["config"]
    assert config["schema_version"] == DASHBOARD_SCHEMA_VERSION
    views = config["views"]
    # Paths stay stable for bookmarks; titles are the user-facing names.
    assert [v["path"] for v in views] == [
        "overview",
        "drives",
        "routes",
        "places",
        "charging",
    ]
    assert [v["title"] for v in views] == [
        "Vehicles",
        "Drives",
        "Fav Routes",
        "Destinations",
        "Charging",
    ]
    # Every tab shows its icon and its name.
    assert all(v.get("show_icon_and_title") is True and v.get("icon") for v in views)

    drives_view = views[1]
    assert drives_view["panel"] is True
    assert len(drives_view["cards"]) == 1
    assert drives_view["cards"][0] == {"type": "custom:rivian-drive-explorer-card"}

    places_view = views[3]
    assert places_view["panel"] is True
    assert len(places_view["cards"]) == 1
    assert places_view["cards"][0] == {"type": "custom:rivian-places-card"}

    routes_view = views[2]
    assert routes_view["panel"] is True
    assert len(routes_view["cards"]) == 1
    assert routes_view["cards"][0] == {"type": "custom:rivian-routes-card"}

    all_cards = _find_cards(views)
    assert not any(c.get("type") == "tile" and "picker" in str(c) for c in all_cards)
    assert not any(c.get("type") == "conditional" for c in all_cards)

    overview_view = views[0]
    assert overview_view["cards"] == [
        {
            "type": "custom:rivian-overview-card",
            "vehicles": [
                {"vin": TEST_VIN, "name": "Rivi", "model": "", "entities": {}}
            ],
            "drives_path": "/rivian-dashboard/drives",
        }
    ]


@pytest.mark.asyncio
async def test_no_vin_vehicle_has_no_explorer_cards() -> None:
    """Without a resolved VIN, no drive-explorer cards render."""
    hass = MagicMock()
    hass.data = {}
    hass.states.async_entity_ids.return_value = ["sensor.rivian_last_drive_efficiency"]
    hass.config_entries.async_entries.return_value = []
    # No unique_id registered for this entity_id -- discovery resolves vin="".
    registry = _MockEntityRegistry()
    saved: dict[str, Any] = {}

    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store(saved),
        ),
        patch(
            "custom_components.rivian.dashboard_generator.er",
            MagicMock(async_get=MagicMock(return_value=registry)),
        ),
    ):
        await async_create_efficiency_dashboard(hass=hass)

    views = saved["lovelace.rivian_dashboard"]["config"]["views"]
    all_cards = _find_cards(views)
    assert not any(
        c.get("type") == "custom:rivian-drive-explorer-card" for c in all_cards
    )
    # The Drives/Places/Routes tabs have nothing to show for a vin-less vehicle.
    drives_view = next(v for v in views if v["path"] == "drives")
    assert drives_view["cards"] == []
    places_view = next(v for v in views if v["path"] == "places")
    assert places_view["cards"] == []
    routes_view = next(v for v in views if v["path"] == "routes")
    assert routes_view["cards"] == []


@pytest.mark.asyncio
async def test_two_vehicles_get_one_card_per_tab_and_no_picker() -> None:
    """Multi-vehicle dashboards have no picker or conditional.

    Drives/Places/Routes hold ONE card (no vin: it follows the shared vehicle
    selection); Charging/Efficiency are panel views holding their one card each;
    Overview has no bar card.
    """
    hass, registry = _hass_with_two_vehicles()
    saved: dict[str, Any] = {}

    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store(saved),
        ),
        patch(
            "custom_components.rivian.dashboard_generator.er",
            MagicMock(async_get=MagicMock(return_value=registry)),
        ),
    ):
        await async_create_efficiency_dashboard(hass=hass)

    views = saved["lovelace.rivian_dashboard"]["config"]["views"]
    all_cards = _find_cards(views)
    assert not any(c.get("type") == "conditional" for c in all_cards)
    assert not any(
        c.get("type") == "tile" and "Dashboard vehicle" in str(c) for c in all_cards
    )

    overview = next(v for v in views if v["path"] == "overview")
    assert len(overview["cards"]) == 1
    assert overview["cards"][0]["type"] == "custom:rivian-overview-card"
    assert {v["vin"] for v in overview["cards"][0]["vehicles"]} == {
        TEST_VIN,
        OTHER_VIN,
    }
    assert not any(
        c.get("type") == "custom:rivian-vehicle-bar-card" for c in _find_cards(overview)
    )

    for path, card_type in (
        ("drives", "custom:rivian-drive-explorer-card"),
        ("places", "custom:rivian-places-card"),
        ("routes", "custom:rivian-routes-card"),
    ):
        view = next(v for v in views if v["path"] == path)
        assert view["panel"] is True
        assert view["cards"] == [{"type": card_type}]

    charging = next(v for v in views if v["path"] == "charging")
    assert charging["panel"] is True
    assert charging["cards"] == [{"type": "custom:rivian-charging-card"}]


@pytest.mark.asyncio
async def test_overview_card_entities_come_from_the_registry() -> None:
    """The Overview card's per-vehicle entities are resolved ids, never guessed."""
    hass = _hass_with_one_vehicle()
    hass.data[DOMAIN]["entry1"][ATTR_VEHICLE]["vehicle1"]["model"] = "R1S"
    saved: dict[str, Any] = {}
    registry = _MockEntityRegistry(
        entity_ids=_core_entity_ids(
            TEST_VIN, "rivi", ["soc", "range", "location", "drive_status"]
        )
    )

    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store(saved),
        ),
        _er_patch(registry),
    ):
        await async_create_efficiency_dashboard(hass=hass)

    overview = saved["lovelace.rivian_dashboard"]["config"]["views"][0]
    card = overview["cards"][0]
    assert card["type"] == "custom:rivian-overview-card"
    [vehicle] = card["vehicles"]
    assert vehicle["vin"] == TEST_VIN
    assert vehicle["name"] == "Rivi"
    assert vehicle["model"] == "R1S"
    assert vehicle["entities"] == {
        "soc": "sensor.rivi_soc",
        "range": "sensor.rivi_range",
        "location": "device_tracker.rivi_location",
        "drive_status": "sensor.rivi_drive_status",
    }
    # Only resolved keys are present -- nothing guessed or empty.
    assert all(v for v in vehicle["entities"].values())


@pytest.mark.asyncio
async def test_charging_tab_is_one_panel_charging_card() -> None:
    """Charging is a panel view holding only the charging card (bar renders inside)."""
    hass = _hass_with_one_vehicle()
    saved: dict[str, Any] = {}
    registry = _MockEntityRegistry(
        entity_ids=_core_entity_ids(TEST_VIN, "rivi", ["charging_rate"])
    )

    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store(saved),
        ),
        _er_patch(registry),
    ):
        await async_create_efficiency_dashboard(hass=hass)

    charging = next(
        v
        for v in saved["lovelace.rivian_dashboard"]["config"]["views"]
        if v["path"] == "charging"
    )
    assert charging["panel"] is True
    assert charging["cards"] == [{"type": "custom:rivian-charging-card"}]


def _all_entity_values(node: Any) -> list[Any]:
    """Collect every "entity" value and every value in an "entities" list, anywhere."""
    found: list[Any] = []
    if isinstance(node, dict):
        if "entity" in node:
            found.append(node["entity"])
        if "entities" in node and isinstance(node["entities"], list):
            found.extend(v for v in node["entities"] if not isinstance(v, (dict, list)))
        for value in node.values():
            found.extend(_all_entity_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_all_entity_values(item))
    return found


@pytest.mark.asyncio
@pytest.mark.parametrize("vehicle_count", [1, 2])
async def test_no_card_has_an_empty_or_missing_entity(vehicle_count: int) -> None:
    """Walk the whole generated dashboard: no "entity" or "entities" value is "" or None."""
    if vehicle_count == 1:
        hass = _hass_with_one_vehicle()
        registry = _MockEntityRegistry(
            entity_ids=_core_entity_ids(TEST_VIN, "rivi", ["soc", "charging_rate"])
        )
    else:
        hass, registry = _hass_with_two_vehicles()
    saved: dict[str, Any] = {}

    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store(saved),
        ),
        _er_patch(registry),
    ):
        await async_create_efficiency_dashboard(hass=hass)

    views = saved["lovelace.rivian_dashboard"]["config"]["views"]
    values = _all_entity_values(views)
    assert not any(v in ("", None) for v in values)


class _LiveDashboard:
    """Stand-in for Lovelace's LovelaceStorage, which serves config from memory."""

    mode = "storage"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.saved: list[dict[str, Any]] = []

    async def async_load(self, force: bool) -> dict[str, Any]:
        return self.config

    async def async_save(self, config: dict[str, Any]) -> None:
        self.saved.append(config)
        self.config = config


@pytest.mark.asyncio
async def test_dashboards_loaded_by_lovelace_are_saved_through_it() -> None:
    """A direct file write is invisible once Lovelace holds the dashboard in memory."""
    efficiency = _LiveDashboard({"views": []})

    hass = MagicMock()
    hass.data = {
        "lovelace": SimpleNamespace(dashboards={"rivian-dashboard": efficiency})
    }
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_last_drive_efficiency",
    ]
    hass.config_entries.async_entries.return_value = []
    file_writes: dict[str, Any] = {}

    registry = _MockEntityRegistry(
        {"sensor.rivian_r1s_last_drive_efficiency": f"{TEST_VIN}-last_drive_efficiency"}
    )
    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store(file_writes),
        ),
        patch(
            "custom_components.rivian.dashboard_generator.er",
            MagicMock(async_get=MagicMock(return_value=registry)),
        ),
    ):
        await async_create_efficiency_dashboard(hass=hass)

    assert len(efficiency.saved) == 1
    assert efficiency.saved[0]["schema_version"] == DASHBOARD_SCHEMA_VERSION
    # The dashboard's storage file wasn't written behind Lovelace's back.
    assert "lovelace.rivian_dashboard" not in file_writes


@pytest.mark.asyncio
@pytest.mark.parametrize("already_live", [True, False])
async def test_restart_notice_only_for_a_brand_new_dashboard(
    already_live: bool,
) -> None:
    """Lovelace only picks up a new dashboard on restart; an existing one is live."""
    hass = MagicMock()
    hass.data = {
        "lovelace": SimpleNamespace(
            dashboards={"rivian-dashboard": _LiveDashboard({"views": []})}
            if already_live
            else {}
        )
    }
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_last_drive_efficiency",
    ]
    hass.config_entries.async_entries.return_value = []
    registry = _MockEntityRegistry(
        {"sensor.rivian_r1s_last_drive_efficiency": f"{TEST_VIN}-last_drive_efficiency"}
    )
    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store({}),
        ),
        patch(
            "custom_components.rivian.dashboard_generator.er",
            MagicMock(async_get=MagicMock(return_value=registry)),
        ),
        patch(
            "custom_components.rivian.dashboard_generator._notify_restart_needed"
        ) as notify,
    ):
        await async_create_efficiency_dashboard(hass=hass)

    assert notify.called is (not already_live)
