"""Unit tests for Lovelace Trip Efficiency & Analytics Dashboard YAML package."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import pytest
import yaml

from custom_components.rivian.const import DRIVE_SENSORS

REPO_ROOT = Path(__file__).parent.parent
DASHBOARD_YAML_PATH = REPO_ROOT / "lovelace_efficiency_dashboard.yaml"

EXPECTED_ENTITY_KEYS = [
    "last_drive_efficiency",
    "efficiency_30d",
    "efficiency_all_time",
    "last_drive_distance",
    "last_drive_mpge",
    "mpge_30d",
    "mpge_all_time",
    "drive_status",
]


EXPECTED_HEX_COLORS = {
    "downhill": "#1E88E5",
    "flat": "#43A047",
    "uphill": "#FB8C00",
}

EXPECTED_SPEED_BINS = [
    "0-9",
    "10-19",
    "20-29",
    "30-39",
    "40-49",
    "50-59",
    "60-69",
    "70-79",
    "80+",
]


@pytest.fixture(scope="module")
def dashboard_raw_content() -> str:
    """Read the raw text content of lovelace_efficiency_dashboard.yaml."""
    assert DASHBOARD_YAML_PATH.is_file(), (
        f"Dashboard YAML file not found at {DASHBOARD_YAML_PATH}"
    )
    return DASHBOARD_YAML_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dashboard_yaml_data(dashboard_raw_content: str) -> dict[str, Any]:
    """Parse and return dashboard YAML data using yaml.safe_load."""
    data = yaml.safe_load(dashboard_raw_content)
    assert isinstance(data, dict), "Dashboard YAML root must be a dictionary"
    return data


def _find_cards(node: Any) -> list[dict[str, Any]]:
    """Recursively collect all card dictionaries from a nested YAML structure."""
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


class TestDashboardYAMLParsing:
    """Validate YAML syntax parsing and high-level dashboard architecture."""

    def test_file_exists_and_non_empty(self, dashboard_raw_content: str) -> None:
        """Verify dashboard YAML file exists and is not empty."""
        assert len(dashboard_raw_content.strip()) > 100

    def test_yaml_safe_load(self, dashboard_yaml_data: dict[str, Any]) -> None:
        """Verify that PyYAML safely loads the file without syntax errors."""
        assert "title" in dashboard_yaml_data
        assert "views" in dashboard_yaml_data
        assert isinstance(dashboard_yaml_data["views"], list)
        assert len(dashboard_yaml_data["views"]) >= 2

    def test_views_structure(self, dashboard_yaml_data: dict[str, Any]) -> None:
        """Verify views define title, path, and cards list."""
        views = dashboard_yaml_data["views"]
        paths = [v.get("path") for v in views]
        assert "rivian-efficiency" in paths
        assert "rivian-efficiency-core" in paths

        for view in views:
            assert "title" in view
            assert "cards" in view
            assert isinstance(view["cards"], list)
            assert len(view["cards"]) > 0

    def test_documentation_and_substitution_instructions(
        self, dashboard_raw_content: str
    ) -> None:
        """Verify header comments provide clear instructions for VIN/prefix replacement."""
        assert "INSTRUCTIONS FOR USE" in dashboard_raw_content
        assert "{vin}" in dashboard_raw_content
        assert "sensor.{vin}_last_drive_efficiency" in dashboard_raw_content
        assert "Mushroom" in dashboard_raw_content
        assert "Plotly Graph Card" in dashboard_raw_content


class TestRequiredCardsPresence:
    """Validate presence and configuration of all required Lovelace cards."""

    def test_overview_card_presence(
        self, dashboard_yaml_data: dict[str, Any], dashboard_raw_content: str
    ) -> None:
        """Validate Efficiency Overview cards (Mushroom hero badge, chips, status)."""
        all_cards = _find_cards(dashboard_yaml_data)
        card_types = [c.get("type") for c in all_cards]

        # Check Mushroom template card (Hero badge)
        assert "custom:mushroom-template-card" in card_types
        # Check Mushroom chips card
        assert "custom:mushroom-chips-card" in card_types

        # Verify hero badge contains mi/kWh and references sensor.{vin}_last_drive_efficiency
        assert "sensor.{vin}_last_drive_efficiency" in dashboard_raw_content
        assert "mi/kWh" in dashboard_raw_content

        # Verify chips contain 30-day, all-time, MPGe, and drive status
        assert "sensor.{vin}_efficiency_30d" in dashboard_raw_content
        assert "sensor.{vin}_efficiency_all_time" in dashboard_raw_content
        assert "sensor.{vin}_last_drive_mpge" in dashboard_raw_content
        assert "sensor.{vin}_drive_status" in dashboard_raw_content

    def test_plotly_scatterplot_card_presence(
        self, dashboard_yaml_data: dict[str, Any], dashboard_raw_content: str
    ) -> None:
        """Validate Plotly temperature vs efficiency scatterplot card."""
        all_cards = _find_cards(dashboard_yaml_data)
        plotly_cards = [
            c
            for c in all_cards
            if c.get("type") in ("custom:plotly-graph", "custom:plotly-graph-card")
        ]
        assert len(plotly_cards) >= 2, "Expected at least 2 Plotly graph cards"

        # Find the scatterplot card
        scatterplot = next(
            (
                c
                for c in plotly_cards
                if "temperature" in str(c.get("title", "")).lower()
                or "scatter" in str(c.get("title", "")).lower()
            ),
            None,
        )
        assert scatterplot is not None, "Scatterplot card not found"

        # Check layout axes
        layout = scatterplot.get("layout", {})
        assert "xaxis" in layout
        assert "yaxis" in layout
        assert (
            "temperature" in str(layout.get("xaxis", {}).get("title", "")).lower()
            or "temp" in str(layout.get("xaxis", {}).get("title", "")).lower()
        )
        assert (
            "efficiency" in str(layout.get("yaxis", {}).get("title", "")).lower()
            or "mi/kwh" in str(layout.get("yaxis", {}).get("title", "")).lower()
        )

        # Check elevation traces
        entities = scatterplot.get("entities", [])
        trace_names = [e.get("name", "") for e in entities if isinstance(e, dict)]
        assert any("downhill" in n.lower() for n in trace_names)
        assert any("flat" in n.lower() for n in trace_names)
        assert any("uphill" in n.lower() for n in trace_names)

    def test_speed_bin_distribution_card_presence(
        self, dashboard_yaml_data: dict[str, Any], dashboard_raw_content: str
    ) -> None:
        """Validate Speed Bin Distribution bar chart card."""
        all_cards = _find_cards(dashboard_yaml_data)
        plotly_cards = [
            c
            for c in all_cards
            if c.get("type") in ("custom:plotly-graph", "custom:plotly-graph-card")
        ]

        speed_bin_card = next(
            (c for c in plotly_cards if "speed bin" in str(c.get("title", "")).lower()),
            None,
        )
        assert speed_bin_card is not None, "Speed bin card not found"

        # Check speed bins categories
        entities = speed_bin_card.get("entities", [])
        assert len(entities) > 0
        speed_trace = entities[0]
        assert speed_trace.get("type") == "bar"

        # Check all 9 speed bins are present in x-axis or trace x
        for bin_label in EXPECTED_SPEED_BINS:
            assert bin_label in dashboard_raw_content, (
                f"Speed bin {bin_label} missing from dashboard"
            )

        # Verify speed_bins attribute extraction reference
        assert "speed_bins" in dashboard_raw_content

    def test_core_fallback_cards_presence(
        self, dashboard_yaml_data: dict[str, Any]
    ) -> None:
        """Validate Home Assistant Core fallback cards (Tile, Entities, Statistics, History)."""
        views = dashboard_yaml_data["views"]
        core_view = next(
            (v for v in views if v.get("path") == "rivian-efficiency-core"), None
        )
        assert core_view is not None, "Core fallback view missing"

        core_cards = _find_cards(core_view)
        core_card_types = {c.get("type") for c in core_cards}

        # Check Tile cards
        assert "tile" in core_card_types, "Tile card missing in core fallback"
        tile_cards = [c for c in core_cards if c.get("type") == "tile"]
        assert len(tile_cards) >= 8, "Expected at least 8 Tile cards for 8 sensors"

        # Check Entities card
        assert "entities" in core_card_types, "Entities card missing in core fallback"

        # Check Statistics Graph card
        assert "statistics-graph" in core_card_types, (
            "Statistics graph card missing in core fallback"
        )
        stats_card = next(c for c in core_cards if c.get("type") == "statistics-graph")
        assert "entities" in stats_card
        assert len(stats_card["entities"]) >= 3

        # Check History Graph card
        assert "history-graph" in core_card_types, (
            "History graph card missing in core fallback"
        )
        hist_card = next(c for c in core_cards if c.get("type") == "history-graph")
        assert "entities" in hist_card
        assert len(hist_card["entities"]) >= 3


class TestColorHexCodes:
    """Validate exact color hex codes for elevation categories."""

    def test_elevation_color_hex_codes(
        self, dashboard_raw_content: str, dashboard_yaml_data: dict[str, Any]
    ) -> None:
        """Verify exact hex codes for Downhill (#1E88E5), Flat (#43A047), Uphill (#FB8C00)."""
        for category, hex_code in EXPECTED_HEX_COLORS.items():
            assert hex_code in dashboard_raw_content, (
                f"Hex code {hex_code} for {category} not found in dashboard YAML"
            )

        all_cards = _find_cards(dashboard_yaml_data)
        plotly_cards = [
            c
            for c in all_cards
            if c.get("type") in ("custom:plotly-graph", "custom:plotly-graph-card")
        ]
        scatterplot = next(
            (
                c
                for c in plotly_cards
                if "temperature" in str(c.get("title", "")).lower()
            ),
            None,
        )
        assert scatterplot is not None
        traces = scatterplot.get("entities", [])
        colors_in_traces = [
            t.get("marker", {}).get("color")
            for t in traces
            if isinstance(t, dict) and "marker" in t
        ]
        for hex_code in EXPECTED_HEX_COLORS.values():
            assert hex_code in colors_in_traces, (
                f"Color {hex_code} not assigned to a scatterplot trace marker"
            )


class TestEntityAndAttributeReferences:
    """Validate all 8 Rivian drive sensor entity IDs and attribute references."""

    def test_all_8_entity_ids_present(self, dashboard_raw_content: str) -> None:
        """Verify that all 8 Rivian drive efficiency sensor entity IDs are present in the YAML."""
        for key in EXPECTED_ENTITY_KEYS:
            expected_entity_id = f"sensor.{{vin}}_{key}"
            assert expected_entity_id in dashboard_raw_content, (
                f"Entity ID {expected_entity_id} missing from dashboard YAML"
            )

    def test_entity_attributes_referenced(self, dashboard_raw_content: str) -> None:
        """Verify references to drive sensor attributes matching sensor.py and drive_tracker.py."""
        # Drive status live attributes
        assert "current_trip_distance_mi" in dashboard_raw_content
        assert "current_trip_duration_s" in dashboard_raw_content
        assert "current_trip_efficiency" in dashboard_raw_content
        assert "current_speed_mph" in dashboard_raw_content
        assert "gps_locked" in dashboard_raw_content

        # Last drive efficiency attributes
        assert "speed_bins" in dashboard_raw_content

    def test_const_drive_sensors_parity(self) -> None:
        """Verify that all entity descriptions in const.DRIVE_SENSORS match EXPECTED_ENTITY_KEYS."""
        const_keys = [desc.key for desc in DRIVE_SENSORS]
        assert set(const_keys) == set(EXPECTED_ENTITY_KEYS)
        assert len(const_keys) == 8

    def test_vin_substitution_integrity(self, dashboard_raw_content: str) -> None:
        """Verify that substituting {vin} produces valid Home Assistant entity IDs without leftovers."""
        test_vins = ["my_rivian", "7pdsgaba8nn000000", "r1s_test"]

        for test_vin in test_vins:
            substituted_content = dashboard_raw_content.replace("{vin}", test_vin)
            assert "{vin}" not in substituted_content

            parsed = yaml.safe_load(substituted_content)
            assert isinstance(parsed, dict)

            # Confirm all 8 substituted entity IDs exist in text
            for key in EXPECTED_ENTITY_KEYS:
                substituted_entity_id = f"sensor.{test_vin}_{key}"
                assert substituted_entity_id in substituted_content

            # Verify no malformed entity ID patterns
            assert not re.search(r"sensor\.[a-zA-Z0-9_]*\{vin\}", substituted_content)
