"""Tests for the tabbed Rivian dashboard generator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian.config_flow import CONF_CHART_WINDOW_DAYS
from custom_components.rivian.const import (
    ATTR_VEHICLE,
    DASHBOARD_SCHEMA_VERSION,
    DOMAIN,
)
from custom_components.rivian.dashboard_generator import (
    ENTITY_KEY_MAP,
    _async_resolve_vehicle_entities,
    _build_core_fallback_view,
    _build_vehicle_analytics_view,
    _collect_chart_cards,
    _collect_vehicle_models,
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


def test_jinja_templates_use_no_javascript_syntax() -> None:
    """`?.` is JavaScript; inside a Jinja template it's an error at render time."""
    view = _build_vehicle_analytics_view("R1S", "sensor.rivian_r1s_", TEST_VIN)
    templates = [s for s in _strings(view) if "{{" in s]

    assert templates, "expected some Jinja templates in the generated cards"
    assert not [t for t in templates if "?." in t]
    # f-string brace escaping leaking into plain strings left "{{% if"
    # in the output, which Jinja renders as an error.
    assert not [t for t in templates if "{{%" in t or "%}}" in t]


def _unwrap(card: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a custom:rivian-series-card down to its underlying plotly config."""
    if card.get("type") == "custom:rivian-series-card":
        return card["card"]
    return card


def test_build_vehicle_analytics_view() -> None:
    """`_build_vehicle_analytics_view` still aggregates every chart for a vehicle.

    This aggregator is no longer used to build the dashboard directly (the
    real dashboard splits it across the Overview/Efficiency/Charging tabs --
    see the class-level tests below) but is kept as a single source of truth
    for "every chart card for one vehicle", relied on by the WebSocket
    field-coverage tests.
    """
    view = _build_vehicle_analytics_view("R1S", "sensor.rivian_r1s_", TEST_VIN)
    assert view["title"] == "R1S Efficiency"
    assert view["path"] == "r1s"
    assert len(view["cards"]) == 10

    hero_card = view["cards"][0]
    assert hero_card["type"] == "vertical-stack"
    template_card = hero_card["cards"][0]
    assert template_card["type"] == "custom:mushroom-template-card"
    assert "sensor.rivian_r1s_last_drive_efficiency" in template_card["primary"]

    scatter_wrapper = view["cards"][1]
    assert scatter_wrapper["type"] == "custom:rivian-series-card"
    assert scatter_wrapper["vin"] == TEST_VIN
    scatter_card = _unwrap(scatter_wrapper)
    assert scatter_card["type"] == "custom:plotly-graph"
    assert len(scatter_card["entities"]) == 3

    stats_grid = view["cards"][9]
    assert stats_grid["type"] == "grid"
    assert len(stats_grid["cards"]) == 6


def test_wrapped_cards_carry_the_requested_days_window() -> None:
    """`days` threads through every wrapped series card."""
    view = _build_vehicle_analytics_view("R1S", "sensor.rivian_r1s_", TEST_VIN, days=42)
    wrapped = [c for c in view["cards"] if c.get("type") == "custom:rivian-series-card"]
    assert wrapped, "expected at least one wrapped series card"
    assert all(c["days"] == 42 for c in wrapped)


def test_build_core_fallback_view() -> None:
    """Test building the zero-dependency Native Core fallback view."""
    view = _build_core_fallback_view("R1S", "sensor.rivian_r1s_")
    assert "Native Core" in view["title"]
    assert view["path"] == "r1s-core"
    assert len(view["cards"]) == 3

    grid_card = view["cards"][0]
    assert grid_card["type"] == "grid"
    assert len(grid_card["cards"]) == 8
    assert grid_card["cards"][0]["type"] == "tile"


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
    picker_entity_id: str | None,
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

    entity_ids = {}
    if picker_entity_id:
        entity_ids[("select", DOMAIN, "entry1-dashboard_vehicle")] = picker_entity_id
    registry = _MockEntityRegistry(entity_ids=entity_ids)
    return hass, registry


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
        "efficiency",
    ]
    assert [v["title"] for v in views] == [
        "Vehicles",
        "Drives",
        "Fav Routes",
        "Destinations",
        "Charging",
        "Efficiency",
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
async def test_efficiency_tab_is_one_panel_efficiency_card() -> None:
    """Efficiency is a panel view with the one card; no Plotly chart is generated."""
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

    views = saved["lovelace.rivian_dashboard"]["config"]["views"]
    # The Plotly charts are gone from the generated dashboard: Efficiency is a
    # panel view holding only the efficiency card.
    assert _collect_chart_cards([v["cards"] for v in views]) == []
    efficiency = next(v for v in views if v["path"] == "efficiency")
    assert efficiency["panel"] is True
    assert efficiency["cards"] == [{"type": "custom:rivian-efficiency-card"}]


@pytest.mark.asyncio
async def test_series_cards_carry_the_configured_chart_window_days() -> None:
    """Injected series cards use the first config entry's chart_window_days option."""
    hass = _hass_with_one_vehicle()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(options={CONF_CHART_WINDOW_DAYS: 90})
    ]
    saved: dict[str, Any] = {
        "lovelace.dashboard_automobiles": {
            "config": {
                "views": [{"sections": [{"cards": []}, {"cards": []}, {"cards": []}]}]
            }
        }
    }
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

    # The Rivian dashboard has no Plotly charts any more; the series cards are
    # still injected into dashboard-automobiles, with the configured window.
    auto_views = saved["lovelace.dashboard_automobiles"]["config"]["views"]
    wrapped = [
        c
        for c in _find_cards(auto_views)
        if c.get("type") == "custom:rivian-series-card"
    ]
    assert wrapped, "expected at least one wrapped series card"
    assert all(c["days"] == 90 for c in wrapped)


@pytest.mark.asyncio
async def test_no_vin_vehicle_has_no_series_or_explorer_cards() -> None:
    """Without a resolved VIN, no bulk-series or drive-explorer cards render."""
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
    assert not any(c.get("type") == "custom:rivian-series-card" for c in all_cards)
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
@pytest.mark.parametrize("picker", ["select.dashboard_vehicle", None])
async def test_two_vehicles_get_one_card_per_tab_and_no_picker(
    picker: str | None,
) -> None:
    """Multi-vehicle dashboards have no picker or conditional, even if the select exists.

    Drives/Places/Routes hold ONE card (no vin: it follows the shared vehicle
    selection); Charging/Efficiency are panel views holding their one card each;
    Overview has no bar card.
    """
    hass, registry = _hass_with_two_vehicles(picker_entity_id=picker)
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
    assert not any(picker and picker in str(c) for c in all_cards)
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

    efficiency = next(v for v in views if v["path"] == "efficiency")
    assert efficiency["panel"] is True
    assert efficiency["cards"] == [{"type": "custom:rivian-efficiency-card"}]

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
        hass, registry = _hass_with_two_vehicles(
            picker_entity_id="select.dashboard_vehicle"
        )
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


DEMO_R2_VIN = "DEMO0R2EAGLE00001"
DEMO_R1T_VIN = "DEMO1R1TEAGLE0002"
DEMO_VEHICLES = [
    {"vin": DEMO_R2_VIN, "name": "Demo R2", "model": "R2"},
    {"vin": DEMO_R1T_VIN, "name": "Demo R1T", "model": "R1T"},
]


def _add_demo_vehicles(hass: MagicMock) -> None:
    hass.data[DOMAIN]["_demo_vehicles"] = [dict(v) for v in DEMO_VEHICLES]


@pytest.mark.asyncio
async def test_discovery_includes_demo_vehicles_without_entities() -> None:
    """Demo vehicles join discovery with name/vin, no entity prefix, and the real entry id."""
    hass = _hass_with_one_vehicle()
    _add_demo_vehicles(hass)

    with _er_patch(_MockEntityRegistry()):
        found = await async_discover_vehicle_prefixes(hass)

    by_vin = {vin: (name, prefix, entry_id) for name, prefix, vin, entry_id in found}
    assert by_vin[TEST_VIN][0] == "Rivi"
    assert by_vin[DEMO_R2_VIN] == ("Demo R2", "", "entry1")
    assert by_vin[DEMO_R1T_VIN] == ("Demo R1T", "", "entry1")
    models = _collect_vehicle_models(hass)
    assert models[DEMO_R2_VIN] == "R2"
    assert models[DEMO_R1T_VIN] == "R1T"
    # The demo registry key is not mistaken for a config entry.
    assert all(not name.startswith("_") for name, *_ in found)


@pytest.mark.asyncio
async def test_demo_vehicles_appear_in_overview_and_stacked_blocks() -> None:
    """One real vehicle + two demo ones: all listed on Overview, stacked under headings."""
    hass = _hass_with_one_vehicle()
    _add_demo_vehicles(hass)
    registry = _MockEntityRegistry(
        entity_ids={
            ("select", DOMAIN, "entry1-dashboard_vehicle"): "select.dashboard_vehicle"
        }
    )
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
    overview = next(v for v in views if v["path"] == "overview")
    listed = {v["vin"]: v for v in overview["cards"][0]["vehicles"]}
    assert set(listed) == {TEST_VIN, DEMO_R2_VIN, DEMO_R1T_VIN}
    assert listed[DEMO_R2_VIN]["name"] == "Demo R2"
    assert listed[DEMO_R2_VIN]["model"] == "R2"
    assert listed[DEMO_R2_VIN]["entities"] == {}

    for view in views:
        if view["path"] != "overview":
            assert len(view["cards"]) == 1

    # Every analytics card for a demo vehicle is keyed by its VIN (the series
    # WebSocket resolves it through _find_store); none reads a missing entity.
    values = _all_entity_values(views)
    assert not any(v in ("", None) for v in values)
    demo_strings = [
        text
        for view in views
        for text in _strings(view)
        if text.startswith("sensor.") and "demo" in text.lower()
    ]
    assert demo_strings == []


async def _create_with_automobiles(
    registry_entries: dict[str, str], section_cards: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run dashboard creation against a stored automobiles dashboard; return section 2."""
    hass = MagicMock()
    hass.data = {}
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_last_drive_efficiency",
    ]
    hass.config_entries.async_entries.return_value = []
    saved: dict[str, Any] = {
        "lovelace.dashboard_automobiles": {
            "config": {
                "views": [
                    {
                        "sections": [
                            {"cards": []},
                            {"cards": []},
                            {"cards": list(section_cards)},
                        ]
                    }
                ]
            }
        }
    }

    with (
        patch(
            "custom_components.rivian.dashboard_generator.Store",
            side_effect=_grid_dashboards_store(saved),
        ),
        patch(
            "custom_components.rivian.dashboard_generator.er",
            MagicMock(
                async_get=MagicMock(return_value=_MockEntityRegistry(registry_entries))
            ),
        ),
    ):
        await async_create_efficiency_dashboard(hass=hass)

    views = saved["lovelace.dashboard_automobiles"]["config"]["views"]
    return views[0]["sections"][2]["cards"]


USER_CARD = {"type": "entities", "entities": ["sensor.outside_temperature"]}
STALE_CHART = {"type": "custom:plotly-graph", "entities": []}


@pytest.mark.asyncio
async def test_regenerating_swaps_automobiles_charts_instead_of_dropping_them() -> None:
    """Stale bare charts are replaced by wrapped ones; the user's own cards stay."""
    cards = await _create_with_automobiles(
        {
            "sensor.rivian_r1s_last_drive_efficiency": f"{TEST_VIN}-last_drive_efficiency"
        },
        [USER_CARD, STALE_CHART],
    )

    assert cards[0] == USER_CARD
    assert STALE_CHART not in cards
    charts = cards[1:]
    assert charts, "generated charts must be injected, not dropped"
    assert all(c["type"] == "custom:rivian-series-card" for c in charts)


@pytest.mark.asyncio
async def test_no_vin_leaves_automobiles_section_untouched() -> None:
    """Without a VIN there are no charts to inject, so existing ones must survive."""
    cards = await _create_with_automobiles({}, [USER_CARD, STALE_CHART])

    assert cards == [USER_CARD, STALE_CHART]


@pytest.mark.asyncio
async def test_automobiles_efficiency_view_gets_only_efficiency_tab_cards() -> None:
    """The injected "efficiency" view in dashboard-automobiles mirrors our Efficiency tab."""
    hass = MagicMock()
    hass.data = {}
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_last_drive_efficiency",
    ]
    hass.config_entries.async_entries.return_value = []
    saved: dict[str, Any] = {
        "lovelace.dashboard_automobiles": {
            "config": {
                "views": [
                    {"sections": [{"cards": []}, {"cards": []}, {"cards": []}]},
                ]
            }
        }
    }
    registry = _MockEntityRegistry(
        {"sensor.rivian_r1s_last_drive_efficiency": f"{TEST_VIN}-last_drive_efficiency"}
    )

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

    auto_views = saved["lovelace.dashboard_automobiles"]["config"]["views"]
    eff_view = next(v for v in auto_views if v.get("path") == "efficiency")
    assert eff_view["title"] == "Efficiency & Analytics"
    assert all(
        c.get("type") != "custom:rivian-drive-explorer-card" for c in eff_view["cards"]
    )


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
    cached_automobiles = {
        "views": [
            {
                "sections": [
                    {"cards": []},
                    {"cards": []},
                    {"cards": [USER_CARD, STALE_CHART]},
                ]
            }
        ]
    }
    automobiles = _LiveDashboard(cached_automobiles)

    hass = MagicMock()
    hass.data = {
        "lovelace": SimpleNamespace(
            dashboards={
                "rivian-dashboard": efficiency,
                "dashboard-automobiles": automobiles,
            }
        )
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
    section = automobiles.saved[0]["views"][0]["sections"][2]["cards"]
    assert section[0] == USER_CARD
    assert all(c["type"] == "custom:rivian-series-card" for c in section[1:])
    # Neither dashboard's storage file was written behind Lovelace's back...
    assert "lovelace.rivian_dashboard" not in file_writes
    assert "lovelace.dashboard_automobiles" not in file_writes
    # ...and Lovelace's cached copy wasn't mutated before being saved.
    assert cached_automobiles["views"][0]["sections"][2]["cards"] == [
        USER_CARD,
        STALE_CHART,
    ]


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


def test_collect_chart_cards_descends_into_conditionals_and_stacks() -> None:
    """`_collect_chart_cards` must find charts nested behind picker conditionals."""
    tree = [
        {"type": "tile", "entity": "select.dashboard_vehicle"},
        {
            "type": "conditional",
            "conditions": [],
            "card": {
                "type": "vertical-stack",
                "cards": [
                    USER_CARD,
                    {"type": "custom:rivian-series-card", "vin": TEST_VIN},
                ],
            },
        },
    ]
    found = _collect_chart_cards(tree)
    assert len(found) == 1
    assert found[0]["type"] == "custom:rivian-series-card"


# NOTE: A test for `select.RivianDashboardVehicleSelect` (options, default
# selection, RestoreEntity restore behavior) was intentionally not added
# here. tests/conftest.py's mock environment doesn't stub
# `homeassistant.helpers.restore_state` or `homeassistant.components.select`
# (select.py is not imported by any existing test), and conftest.py is
# outside this task's ownership, so adding those mocks wasn't done. The class
# itself is a thin, self-contained SelectEntity/RestoreEntity subclass in
# custom_components/rivian/select.py.
