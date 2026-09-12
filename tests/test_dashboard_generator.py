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
    view = _build_vehicle_analytics_view("R1S", "sensor.rivian_r1s_")
    assert view["title"] == "R1S Efficiency"
    assert view["path"] == "r1s"
    assert len(view["cards"]) == 10

    # Check Mushroom Card
    hero_card = view["cards"][0]
    assert hero_card["type"] == "vertical-stack"
    template_card = hero_card["cards"][0]
    assert template_card["type"] == "custom:mushroom-template-card"
    assert "sensor.rivian_r1s_last_drive_efficiency" in template_card["primary"]

    # Check Plotly Scatterplot
    scatter_card = view["cards"][1]
    assert scatter_card["type"] == "custom:plotly-graph"
    assert len(scatter_card["entities"]) == 3
    assert scatter_card["entities"][0]["name"] == "Downhill (Δh < -100 ft)"
    assert scatter_card["entities"][0]["type"] == "scatter"
    assert scatter_card["entities"][0]["entity"] == ""
    assert scatter_card["entities"][1]["name"] == "Flat (-100 to +100 ft)"
    assert scatter_card["entities"][2]["name"] == "Uphill (Δh > +100 ft)"

    # Check Distance vs Efficiency Scatterplot Card
    dist_eff_card = view["cards"][2]
    assert dist_eff_card["type"] == "custom:plotly-graph"
    assert dist_eff_card["title"] == "Drive Distance vs. Efficiency"
    assert len(dist_eff_card["entities"]) == 3
    assert dist_eff_card["entities"][0]["name"] == "Downhill (Δh < -100 ft)"
    assert dist_eff_card["entities"][0]["type"] == "scatter"
    assert dist_eff_card["entities"][1]["name"] == "Flat (-100 to +100 ft)"
    assert dist_eff_card["entities"][2]["name"] == "Uphill (Δh > +100 ft)"

    # Check Speed Bin Card
    speed_card = view["cards"][3]
    assert speed_card["type"] == "custom:plotly-graph"
    assert speed_card["entities"][0]["type"] == "bar"
    assert speed_card["entities"][0]["entity"] == ""

    # Check Speed Range vs Trip Efficiency Box Plot Card
    speed_eff_card = view["cards"][4]
    assert speed_eff_card["type"] == "custom:plotly-graph"
    assert len(speed_eff_card["entities"]) == 2
    assert speed_eff_card["entities"][0]["type"] == "box"
    assert speed_eff_card["entities"][0]["name"] == "Uphill (+)"
    assert speed_eff_card["entities"][0]["marker"]["symbol"] == "cross"
    assert speed_eff_card["entities"][0]["boxpoints"] == "all"
    assert speed_eff_card["entities"][1]["type"] == "box"
    assert speed_eff_card["entities"][1]["name"] == "Downhill (o)"
    assert speed_eff_card["entities"][1]["marker"]["symbol"] == "circle"
    assert speed_eff_card["entities"][1]["boxpoints"] == "all"

    # Check MPGe Distribution by Speed Range Box Plot Card
    mpge_card = view["cards"][5]
    assert mpge_card["type"] == "custom:plotly-graph"
    assert mpge_card["title"] == "MPGe Distribution by Speed Range (Box Plot)"
    assert len(mpge_card["entities"]) == 2
    assert mpge_card["entities"][0]["type"] == "box"
    assert mpge_card["entities"][0]["name"] == "Uphill (+)"
    assert mpge_card["entities"][1]["name"] == "Downhill (o)"

    # Check Vampire Drain vs. Time Idle Card
    vampire_card = view["cards"][6]
    assert vampire_card["type"] == "custom:plotly-graph"
    assert vampire_card["title"] == "Vampire Drain vs. Time Idle"
    assert len(vampire_card["entities"]) == 1
    assert vampire_card["entities"][0]["name"] == "Parked Drain Event"
    assert vampire_card["entities"][0]["type"] == "scatter"
    assert vampire_card["entities"][0]["mode"] == "markers"
    assert vampire_card["entities"][0]["marker"]["colorscale"] == "Bluered"
    assert vampire_card["entities"][0]["marker"]["showscale"] is True

    # Check Vampire Drain Rate vs. Ambient Temperature Card
    vampire_temp_card = view["cards"][7]
    assert vampire_temp_card["type"] == "custom:plotly-graph"
    assert vampire_temp_card["title"] == "Vampire Drain Rate vs. Ambient Temperature"
    assert len(vampire_temp_card["entities"]) == 1
    assert vampire_temp_card["entities"][0]["name"] == "Parked Drain Rate"
    assert vampire_temp_card["entities"][0]["marker"]["colorscale"] == "Viridis"

    # Check DC Fast Charging Curves Card
    dcfc_card = view["cards"][8]
    assert dcfc_card["type"] == "custom:plotly-graph"
    assert dcfc_card["title"] == "DC Fast Charging Curves (Power vs. Battery SoC)"
    assert len(dcfc_card["entities"]) == 12
    # Check first individual session trace
    assert dcfc_card["entities"][0]["name"].startswith("$ex")
    assert dcfc_card["entities"][0]["mode"] == "lines+markers"
    assert dcfc_card["entities"][0]["line"]["color"] == "#00E5FF"
    assert dcfc_card["entities"][0]["marker"]["color"] == "#00E5FF"
    # Check average trace
    assert dcfc_card["entities"][10]["name"] == "Average DCFC Curve"
    assert dcfc_card["entities"][10]["line"]["color"] == "#FFFFFF"
    assert dcfc_card["entities"][10]["marker"]["color"] == "#FFFFFF"
    # Check benchmark reference trace (dynamic for pack type)
    assert dcfc_card["entities"][11]["name"].startswith("$ex")
    assert dcfc_card["entities"][11]["line"]["dash"] == "dot"

    # Check Detailed Statistics Grid
    stats_grid = view["cards"][9]
    assert stats_grid["type"] == "grid"
    assert len(stats_grid["cards"]) == 6



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


@pytest.mark.asyncio
async def test_async_discover_vehicle_prefixes() -> None:
    """Test vehicle prefix discovery from Home Assistant state machine."""
    hass = MagicMock()
    hass.data = {}
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_last_drive_efficiency",
        "sensor.rivian_r1s_battery_state_of_charge",
    ]

    prefixes = await async_discover_vehicle_prefixes(hass)
    assert len(prefixes) == 1
    name, prefix = prefixes[0]
    assert "Rivian R1S" in name
    assert prefix == "sensor.rivian_r1s_"


@pytest.mark.asyncio
async def test_async_create_efficiency_dashboard() -> None:
    """Test full turnkey dashboard creation and storage persistence."""
    hass = MagicMock()
    hass.data = {}
    hass.states.async_entity_ids.return_value = [
        "sensor.rivian_r1s_last_drive_efficiency",
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
