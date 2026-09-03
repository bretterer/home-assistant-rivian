"""Tests for the Turnkey Dashboard Generator."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.rivian.dashboard_generator import (
    _build_core_fallback_view,
    _build_vehicle_analytics_view,
    async_create_efficiency_dashboard,
    async_discover_vehicle_prefixes,
)


def test_build_vehicle_analytics_view() -> None:
    """Test building the Plotly and Mushroom analytics view."""
    view = _build_vehicle_analytics_view("Reggie", "sensor.rivian_r1s_reggie_")
    assert view["title"] == "Reggie Efficiency"
    assert view["path"] == "reggie"
    assert len(view["cards"]) == 4

    # Check Mushroom Card
    hero_card = view["cards"][0]
    assert hero_card["type"] == "vertical-stack"
    template_card = hero_card["cards"][0]
    assert template_card["type"] == "custom:mushroom-template-card"
    assert "sensor.rivian_r1s_reggie_last_drive_efficiency" in template_card["primary"]

    # Check Plotly Scatterplot
    scatter_card = view["cards"][1]
    assert scatter_card["type"] == "custom:plotly-graph-card"
    assert len(scatter_card["entities"]) == 3
    assert scatter_card["entities"][0]["name"] == "Downhill (Δh < -100 ft)"
    assert scatter_card["entities"][1]["name"] == "Flat (-100 to +100 ft)"
    assert scatter_card["entities"][2]["name"] == "Uphill (Δh > +100 ft)"

    # Check Speed Bin Card
    speed_card = view["cards"][2]
    assert speed_card["type"] == "custom:plotly-graph-card"
    assert speed_card["entities"][0]["type"] == "bar"
    assert "function(k)" in speed_card["entities"][0]["y"]
    assert "=> {{" not in speed_card["entities"][0]["y"]


def test_build_core_fallback_view() -> None:
    """Test building the zero-dependency Native Core fallback view."""
    view = _build_core_fallback_view("Reggie", "sensor.rivian_r1s_reggie_")
    assert "Native Core" in view["title"]
    assert view["path"] == "reggie-core"
    assert len(view["cards"]) == 3

    grid_card = view["cards"][0]
    assert grid_card["type"] == "grid"
    assert len(grid_card["cards"]) == 8
    assert grid_card["cards"][0]["type"] == "tile"


@pytest.mark.asyncio
async def test_async_discover_vehicle_prefixes() -> None:
    """Test vehicle prefix discovery from Home Assistant state machine."""
    hass = MagicMock()
    hass.data = {}
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_reggie_last_drive_efficiency",
        "sensor.rivian_r1s_reggie_battery_state_of_charge",
    ]

    prefixes = await async_discover_vehicle_prefixes(hass)
    assert len(prefixes) == 1
    name, prefix = prefixes[0]
    assert "Reggie" in name
    assert prefix == "sensor.rivian_r1s_reggie_"


@pytest.mark.asyncio
async def test_async_create_efficiency_dashboard() -> None:
    """Test full turnkey dashboard creation and storage persistence."""
    hass = MagicMock()
    hass.data = {}
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_reggie_last_drive_efficiency",
    ]

    mock_saved_data = {}

    class MockStore:
        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_load(self):
            return mock_saved_data.get(self.key, {"items": []})

        async def async_save(self, data):
            mock_saved_data[self.key] = data

    with patch("custom_components.rivian.dashboard_generator.Store", side_effect=MockStore):
        result = await async_create_efficiency_dashboard(
            hass=hass,
            title="Custom Rivian Dashboard",
            icon="mdi:car",
            url_path="custom-rivian",
        )

        assert result is True
        # Check lovelace_dashboards registry
        dashboards = mock_saved_data["lovelace_dashboards"]["items"]
        assert len(dashboards) == 1
        assert dashboards[0]["url_path"] == "custom-rivian"
        assert dashboards[0]["title"] == "Custom Rivian Dashboard"
        assert dashboards[0]["icon"] == "mdi:car"

        # Check lovelace.custom_rivian view config
        config = mock_saved_data["lovelace.custom_rivian"]["config"]
        assert config["title"] == "Custom Rivian Dashboard"
        assert len(config["views"]) == 2  # 1 analytics view + 1 core view
