"""Tests for the Rivian efficiency dashboard generator's output structure.

This module used to validate the hand-maintained `lovelace_efficiency_dashboard.yaml`
copy/paste template. That template (and its `dashboards/efficiency_dashboard.yaml`
twin) is now a deprecated, non-functional stub -- see `dashboards/README.md`. The
supported path is the `rivian.create_efficiency_dashboard` service, implemented in
`custom_components/rivian/dashboard_generator.py`, so these tests exercise that
generator's output instead of a static YAML file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from custom_components.rivian.const import DASHBOARD_SCHEMA_VERSION
from custom_components.rivian.dashboard_generator import (
    _build_core_fallback_view,
    _build_vehicle_analytics_view,
)

REPO_ROOT = Path(__file__).parent.parent
DEPRECATED_YAML_PATHS = [
    REPO_ROOT / "lovelace_efficiency_dashboard.yaml",
    REPO_ROOT / "dashboards" / "efficiency_dashboard.yaml",
]

TEST_VIN = "7PDSGABA8NN000000"
TEST_PREFIX = "sensor.rivian_r1s_"
TEST_VEHICLE_NAME = "R1S"


def _find_cards(node: Any) -> list[dict[str, Any]]:
    """Recursively collect all card dictionaries from a nested Lovelace structure."""
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


def _build_dashboard_config() -> dict[str, Any]:
    """Assemble a dashboard config the same way async_create_efficiency_dashboard does."""
    analytics_view = _build_vehicle_analytics_view(
        TEST_VEHICLE_NAME, TEST_PREFIX, TEST_VIN
    )
    core_view = _build_core_fallback_view(TEST_VEHICLE_NAME, TEST_PREFIX)
    return {
        "title": "Rivian Efficiency",
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "views": [analytics_view, core_view],
    }


class TestDashboardSchemaVersion:
    """Validate the generated dashboard config carries the current schema version."""

    def test_schema_version_present_and_matches_const(self) -> None:
        """Verify schema_version is present and equals const.DASHBOARD_SCHEMA_VERSION."""
        config = _build_dashboard_config()
        assert isinstance(DASHBOARD_SCHEMA_VERSION, int)
        assert "schema_version" in config
        assert config["schema_version"] == DASHBOARD_SCHEMA_VERSION


class TestDashboardViewsAndCards:
    """Validate the high-level shape of the generated dashboard."""

    def test_expected_views_exist(self) -> None:
        """Verify both the analytics view and the core-fallback view are present."""
        config = _build_dashboard_config()
        views = config["views"]
        assert len(views) == 2

        paths = [v.get("path") for v in views]
        assert "r1s" in paths
        assert "r1s-core" in paths

        for view in views:
            assert "title" in view
            assert "cards" in view
            assert isinstance(view["cards"], list)
            assert len(view["cards"]) > 0

    def test_analytics_view_contains_wrapped_series_cards(self) -> None:
        """Verify chart cards are wrapped in the custom:rivian-series-card element.

        The wrapper card is what fetches bulk drive/chunk/vampire/DCFC series
        data over WebSocket (replacing the removed sensor attribute payloads),
        so every wrapped card must carry a real vehicle VIN and a non-empty
        series list identifying which dataset it needs.
        """
        analytics_view = _build_vehicle_analytics_view(
            TEST_VEHICLE_NAME, TEST_PREFIX, TEST_VIN
        )
        all_cards = _find_cards(analytics_view)
        wrapped_cards = [
            c for c in all_cards if c.get("type") == "custom:rivian-series-card"
        ]
        assert len(wrapped_cards) > 0, (
            "Expected at least one custom:rivian-series-card wrapper"
        )

        for card in wrapped_cards:
            assert card.get("vin") == TEST_VIN
            assert card["vin"], "wrapped card must carry a non-empty vin"
            assert isinstance(card.get("series"), list)
            assert len(card["series"]) > 0, (
                "wrapped card must carry a non-empty series list"
            )
            assert "card" in card, (
                "wrapped card must carry the underlying plotly card config"
            )

    def test_core_fallback_view_has_no_custom_cards(self) -> None:
        """Verify the zero-dependency core view never uses custom: card types."""
        core_view = _build_core_fallback_view(TEST_VEHICLE_NAME, TEST_PREFIX)
        all_cards = _find_cards(core_view)
        card_types = {c.get("type") for c in all_cards if c.get("type")}
        assert not any(t.startswith("custom:") for t in card_types)


class TestDeprecatedYamlStubs:
    """Verify the retired hand-maintained templates are inert, valid stubs."""

    def test_stub_files_are_harmless_yaml_comment_blocks(self) -> None:
        """Verify both retired templates parse to None and point at the service."""
        for path in DEPRECATED_YAML_PATHS:
            assert path.is_file(), f"Expected stub file at {path}"
            content = path.read_text(encoding="utf-8")

            parsed = yaml.safe_load(content)
            assert parsed is None, (
                f"{path.name} should be a comment-only stub (parses to None)"
            )

            assert "deprecated" in content.lower()
            assert "rivian.create_efficiency_dashboard" in content
